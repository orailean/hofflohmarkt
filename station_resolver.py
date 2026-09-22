"""Resolve flyer transit icons to named OpenStreetMap stations."""

from dataclasses import dataclass
import itertools
import math
import re
import unicodedata

import numpy as np


@dataclass(frozen=True)
class StationResolution:
    stations: list[dict]
    warnings: list[str]


def _normalized(text):
    value = unicodedata.normalize("NFKD", str(text)).casefold()
    return re.sub(r"[^a-z0-9]+", "", value)


def _mode(kind):
    if str(kind).startswith("S-Bahn"):
        return "S"
    if str(kind).startswith("U-Bahn"):
        return "U"
    return ""


def _supports_mode(candidate, mode):
    tags = candidate["tags"]
    values = " ".join(str(tags.get(key, "")) for key in (
        "station", "railway", "public_transport", "subway", "train",
        "tram", "light_rail", "network", "operator", "operator:short",
    )).casefold()
    if mode == "S":
        return any(term in values for term in (
            "rail", "train", "s-bahn", "sbahn"))
    if mode == "U":
        return any(term in values for term in (
            "subway", "u-bahn", "ubahn", "stadtbahn", "light_rail"))
    return True


def _element_candidate(element):
    tags = element.get("tags") or {}
    name = tags.get("name") or element.get("name")
    center = element.get("center") or {}
    lat = element.get("lat", center.get("lat"))
    lon = element.get("lon", center.get("lon"))
    if not name or lat is None or lon is None:
        return None
    element_type = element.get("type") or element.get("osm_type") or "item"
    element_id = element.get("id", element.get("osm_id", "unknown"))
    return {
        "name": str(name),
        "lat": float(lat),
        "lon": float(lon),
        "osm_id": f"{element_type}/{element_id}",
        "tags": tags,
    }


def _deduplicate(elements):
    candidates = []
    for element in elements:
        candidate = _element_candidate(element)
        if candidate is None:
            continue
        duplicate = next((
            existing for existing in candidates
            if _normalized(existing["name"]) == _normalized(candidate["name"])
            and _distance_m(existing, candidate) < 150
        ), None)
        if duplicate is None:
            candidates.append(candidate)
            continue
        existing_rank = _candidate_rank(duplicate)
        candidate_rank = _candidate_rank(candidate)
        if candidate_rank > existing_rank:
            candidates[candidates.index(duplicate)] = candidate
    return candidates


def _candidate_rank(candidate):
    railway = str(candidate["tags"].get("railway", ""))
    return {"station": 3, "halt": 2}.get(railway, 1)


def _distance_m(first, second):
    mean_lat = math.radians((first["lat"] + second["lat"]) / 2)
    dy = (first["lat"] - second["lat"]) * 111_320
    dx = (first["lon"] - second["lon"]) * 111_320 * math.cos(mean_lat)
    return math.hypot(dx, dy)


def _fit_icon_locations(icons, control_points):
    pixels = np.array([
        [point["px"], point["py"], 1.0] for point in control_points
    ])
    locations = np.array([
        [point["lat"], point["lon"]] for point in control_points
    ])
    transform, *_ = np.linalg.lstsq(pixels, locations, rcond=None)
    icon_pixels = np.array([[icon[1], icon[2], 1.0] for icon in icons])
    return icon_pixels @ transform


def _local_xy(locations):
    locations = np.asarray(locations, dtype=float)
    mean_lat = math.radians(float(locations[:, 0].mean()))
    return np.column_stack((
        locations[:, 1] * 111_320 * math.cos(mean_lat),
        locations[:, 0] * 111_320,
    ))


def _layout_score(predicted, assignment, context):
    candidate_locations = np.array([
        [candidate["lat"], candidate["lon"]] for candidate in assignment
    ])
    predicted_xy = _local_xy(predicted)
    candidate_xy = _local_xy(candidate_locations)
    predicted_centered = predicted_xy - predicted_xy.mean(axis=0)
    candidate_centered = candidate_xy - candidate_xy.mean(axis=0)
    denominator = float((predicted_centered ** 2).sum())
    scale = (float((predicted_centered * candidate_centered).sum()) /
             denominator) if denominator else 1.0
    if scale <= 0:
        return float("inf")
    residual = np.sqrt(np.mean(
        ((predicted_centered * scale - candidate_centered) ** 2).sum(axis=1)
    ))
    spread = max(100.0, float(np.sqrt(np.mean(
        (candidate_centered ** 2).sum(axis=1)))))
    score = residual / spread
    context_name = _normalized(str(context).split(",", 1)[0])
    if context_name and _normalized(assignment[0]["name"]) == context_name:
        score -= 0.2
    return score


def _match(icons, control_points, context, candidates):
    if not icons:
        return StationResolution([], [])
    predicted = _fit_icon_locations(icons, control_points)
    eligible = [
        [candidate for candidate in candidates
         if _supports_mode(candidate, _mode(icon[0]))]
        for icon in icons
    ]
    if any(not group for group in eligible):
        return StationResolution([], [
            "Station name unavailable for one or more transit icons"
        ])

    context_name = _normalized(str(context).split(",", 1)[0])
    shortlists = []
    for icon_index, group in enumerate(eligible):
        predicted_item = {
            "lat": float(predicted[icon_index, 0]),
            "lon": float(predicted[icon_index, 1]),
        }
        ranked = sorted(group, key=lambda candidate: (
            0 if _normalized(candidate["name"]) == context_name else 1,
            _distance_m(predicted_item, candidate),
        ))
        shortlists.append(ranked[:12])

    scored = []
    for assignment in itertools.product(*shortlists):
        osm_ids = [candidate["osm_id"] for candidate in assignment]
        names = [_normalized(candidate["name"]) for candidate in assignment]
        if len(set(osm_ids)) != len(osm_ids) or len(set(names)) != len(names):
            continue
        score = _layout_score(predicted, assignment, context)
        if np.isfinite(score):
            scored.append((score, assignment))
    if not scored:
        return StationResolution([], [
            "Station name unavailable for one or more transit icons"
        ])
    scored.sort(key=lambda item: item[0])
    best_score, best = scored[0]
    if len(scored) > 1 and scored[1][0] - best_score < 0.02:
        return StationResolution([], [
            "Station name unavailable: transit matches are ambiguous"
        ])

    stations = []
    for icon, candidate in zip(icons, best):
        stations.append({
            "name": candidate["name"],
            "mode": _mode(icon[0]),
            "px": float(icon[1]),
            "py": float(icon[2]),
            "lat": candidate["lat"],
            "lon": candidate["lon"],
            "osm_id": candidate["osm_id"],
        })
    return StationResolution(stations, [])


def resolve_station_icons(icons, control_points, context,
                          candidate_providers):
    """Resolve all icons jointly, trying free-data providers in order."""
    all_elements = []
    provider_errors = []
    for provider in candidate_providers:
        try:
            elements = provider(context, control_points, icons) or []
        except Exception as exc:
            provider_errors.append(str(exc))
            continue
        if not elements:
            continue
        all_elements.extend(elements)
        result = _match(
            icons, control_points, context, _deduplicate(elements))
        if result.stations:
            return result
    if not all_elements:
        detail = f" ({'; '.join(provider_errors)})" if provider_errors else ""
        return StationResolution([], [
            f"Station name unavailable for detected transit icon{detail}"
        ])
    return _match(icons, control_points, context, _deduplicate(all_elements))
