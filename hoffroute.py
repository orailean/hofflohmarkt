#!/usr/bin/env python3
"""
hoffroute.py - Plan the shortest walking route over all market dots in a
Hofflohmaerkte map PDF.

Pipeline:
  1. Render the PDF page and detect the red/pink market dots (color threshold
     + distance-transform peak splitting for touching dots).
  2. Extract the printed street network and connect every market marker to it.
  3. Solve the TSP on that flyer graph for up to three variants:
       A. open path between two stations (best pair, or --start/--end)
       B. closed loop from/to one station
       C. shortest free circle over the dots only (no fixed start/end;
          always produced, and the only variant if no stations are given)
  4. Draw the flyer-graph route on the PDF. With calibration, georeference the
     same stop order and fetch walking geometry from the free FOSSGIS OSRM
     foot router for GPS exports.
  5. Export: annotated PDF/PNG and, when GPS routing validates, GPX, KML,
     GeoJSON, Google Maps links, and a self-contained Leaflet HTML map.

Calibration JSON format:
{
  "map_bbox_px": [x0, y0, x1, y1],          // dot detection area at --dpi
  "control_points": [                        // >= 3, pixel at --dpi -> WGS84
    {"name": "...", "px": 947.9, "py": 979.9, "lat": 48.13566, "lon": 11.59790}
  ],
  "stations": [                              // route start/end candidates
    {"name": "Max-Weber-Platz (U)", "px": 947.9, "py": 979.9,
     "lat": 48.13566, "lon": 11.59790}
  ]
}

Usage:
  python3 hoffroute.py map.pdf --calib calib.json -o out/
  python3 hoffroute.py map.pdf --calib calib.json --start "Max-Weber-Platz (U)" --end "Ostbahnhof (S)"
"""

import argparse
from dataclasses import dataclass
import json
import math
import ssl
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # fall back to system certs
    SSL_CTX = ssl.create_default_context()

import fitz  # PyMuPDF
import numpy as np
from scipy import ndimage

from flyer_streets import FlyerStreetError, FlyerStreetGraph

EARTH_R = 6371000.0


# ----------------------------------------------------------------------------
# 1. dot detection
# ----------------------------------------------------------------------------

def render_page(pdf_path, dpi):
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return doc, img[..., :3].astype(int)


def detect_dots(img, bbox, min_radius_px=6, merge_dist_px=18, isolation_factor=6.0):
    """Detect pink/red market dots inside bbox; split touching clusters.
    Very isolated dots are only dropped when they look like the legend marker:
    a dot surrounded by nearby magenta legend text. This keeps legitimate
    isolated courtyards on sparse map edges."""
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    mask = (r > 160) & (g < 80) & (b > 60) & (b < 150)
    box = np.zeros_like(mask)
    x0, y0, x1, y1 = bbox
    box[y0:y1, x0:x1] = True
    mask &= box

    dist = ndimage.distance_transform_edt(mask)
    mx = ndimage.maximum_filter(dist, size=2 * merge_dist_px + 1)
    py, px = np.where((dist == mx) & (dist > min_radius_px))

    pts, used = [], np.zeros(len(px), bool)
    for i in np.argsort(-dist[py, px]):
        if used[i]:
            continue
        used |= (px - px[i]) ** 2 + (py - py[i]) ** 2 < merge_dist_px ** 2
        pts.append((float(px[i]), float(py[i])))

    if len(pts) > 2:
        # Label pink blobs once for size-based logo detection.
        # Market dots are tiny circles (~200–800 px²); logo artwork (e.g. a
        # city-crest castle silhouette) is a much larger connected pink blob.
        _blob_lab, _n_blobs = ndimage.label(mask)
        _blob_sizes = (np.bincount(_blob_lab.ravel())[1:]
                       if _n_blobs > 0 else np.array([]))

        def is_logo_blob(p):
            """True when the pink blob at this position is far too large to be
            a market dot (threshold: 2500 px² at detect dpi)."""
            x, y = int(p[0]), int(p[1])
            lbl = int(_blob_lab[y, x])
            if lbl == 0 or lbl > len(_blob_sizes):
                return False
            return int(_blob_sizes[lbl - 1]) > 2500

        def legend_text_signal(p):
            x, y = map(int, p)
            y0, y1 = max(0, y - 75), min(mask.shape[0], y + 75)
            x0, x1 = max(0, x - 260), min(mask.shape[1], x + 260)
            region = mask[y0:y1, x0:x1].copy()
            yy, xx = np.ogrid[y0:y1, x0:x1]
            # Ignore the marker itself and nearby market dots; legend text is
            # made of small magenta strokes spread around the marker.
            region[((xx - x) ** 2 + (yy - y) ** 2) < 42 ** 2] = False
            lab, n = ndimage.label(region)
            text_like_pixels = 0
            text_like_components = 0
            for k in range(1, n + 1):
                ys, xs = np.where(lab == k)
                area = len(xs)
                if area < 4:
                    continue
                w, h = np.ptp(xs) + 1, np.ptp(ys) + 1
                # Dots are large, roughly round components. Legend letters are
                # smaller strokes, often narrow or elongated.
                if area < 180 and (w < 28 or h < 28 or w / max(h, 1) > 1.8):
                    text_like_pixels += area
                    text_like_components += 1
            return text_like_components, text_like_pixels

        def looks_like_legend_marker(p, strong=False):
            components, pixels = legend_text_signal(p)
            if strong:
                return components >= 12 and pixels >= 600
            return components >= 4 and pixels >= 80

        def has_dark_text_nearby(p):
            """Sponsor logos have dense dark text around them. Uses a broad
            luminance check (avg RGB < 100) to catch dark-maroon logo text
            (e.g. 'STADT WÜRZBURG') not just pure black. Applied
            unconditionally — a real market dot is never inside a logo."""
            x, y = int(p[0]), int(p[1])
            y0d = max(0, y - 140)
            y1d = min(img.shape[0], y + 140)
            x0d = max(0, x - 200)
            x1d = min(img.shape[1], x + 200)
            patch = img[y0d:y1d, x0d:x1d]
            # avg < 100 catches pure black, dark grey, and dark-saturated colors
            avg = patch.mean(axis=2)
            # exclude pink market-dot pixels so they don't inflate the count
            is_pink = ((patch[..., 0] > 150) & (patch[..., 1] < 90) &
                       (patch[..., 2] > 50))
            dark = (avg < 100) & ~is_pink
            return int(dark.sum()) > 4000

        arr = np.array(pts)
        d = np.sqrt(((arr[:, None] - arr[None, :]) ** 2).sum(axis=2))
        np.fill_diagonal(d, np.inf)
        nn = d.min(axis=1)
        threshold = isolation_factor * float(np.median(nn))
        pts = [p for p, nd in zip(pts, nn)
               if not is_logo_blob(p) and
               not has_dark_text_nearby(p) and
               not looks_like_legend_marker(p, strong=True) and
               (nd <= threshold or not looks_like_legend_marker(p))]

    return pts


def detect_station_icons(img):
    """Find U-Bahn (blue square) and S-Bahn (green circle) icons by color.
    Returns [(kind, x, y), ...] sorted top-to-bottom."""
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    masks = {
        "U-Bahn (blue icon)": (b > 140) & (r < 60) & (g < 110),
        "S-Bahn (green icon)": (g > 110) & (r < 110) & (b < 115) & (g - b > 20),
    }
    found = []
    for kind, mask in masks.items():
        lab, n = ndimage.label(mask)
        for i in range(1, n + 1):
            ys, xs = np.where(lab == i)
            w, h = np.ptp(xs) + 1, np.ptp(ys) + 1
            # station icons are small, roughly square blobs
            if 200 < len(xs) < 3000 and 20 < w < 60 and 20 < h < 60 and 0.6 < w / h < 1.6:
                found.append((kind, float(xs.mean()), float(ys.mean())))
    return sorted(found, key=lambda t: t[2])


def find_landmarks(pdf_path, out_dir, dpi):
    """Helper mode: detect station icons, write a calib template + render PNG."""
    doc, img = render_page(pdf_path, dpi)
    icons = detect_station_icons(img)

    png = Path(out_dir) / "map_render.png"
    doc[0].get_pixmap(dpi=dpi).save(png)

    entries = [{"name": f"FILL ME ({kind} at {x:.0f},{y:.0f})",
                "px": round(x, 1), "py": round(y, 1), "lat": None, "lon": None}
               for kind, x, y in icons]
    h, w = img.shape[:2]
    template = {
        "comment": f"Generated at {dpi} dpi from {Path(pdf_path).name}. "
                   "Fill in names + lat/lon (openstreetmap.org right-click -> "
                   "'Show address'), adjust map_bbox_px to the map area, then "
                   "copy the finished control points into 'stations'.",
        "map_bbox_px": [0, 0, w, h],
        "control_points": entries,
        "stations": entries,
    }
    tpl = Path(out_dir) / "calib_template.json"
    tpl.write_text(json.dumps(template, indent=2, ensure_ascii=False))

    print(f"{len(icons)} station icon candidate(s) at {dpi} dpi:")
    for kind, x, y in icons:
        print(f"    {kind:22s} px=({x:7.1f}, {y:7.1f})")
    print(f"\nWrote {tpl} and {png}.")
    print("Open the PNG to identify each icon, fill in the template, "
          "then run again with --calib.")


# ----------------------------------------------------------------------------
# 2. georeferencing
# ----------------------------------------------------------------------------

def fit_affine(control_points):
    """Least-squares affine pixel->(lat,lon). Returns forward function."""
    P = np.array([[c["px"], c["py"], 1.0] for c in control_points])
    L = np.array([[c["lat"], c["lon"]] for c in control_points])
    A, *_ = np.linalg.lstsq(P, L, rcond=None)

    def px2ll(x, y):
        lat, lon = np.array([x, y, 1.0]) @ A
        return float(lat), float(lon)

    resid = np.array([px2ll(c["px"], c["py"]) for c in control_points]) - L
    rms_m = math.sqrt(np.mean(np.sum((resid * [111320, 111320 * 0.667]) ** 2, axis=1)))
    return px2ll, rms_m


def fit_reverse_affine(control_points):
    """Fit the inverse affine transform from WGS84 coordinates to pixels."""
    locations = np.array([
        [c["lat"], c["lon"], 1.0] for c in control_points
    ])
    pixels = np.array([[c["px"], c["py"]] for c in control_points])
    transform, *_ = np.linalg.lstsq(locations, pixels, rcond=None)

    def ll2px(lat, lon):
        x, y = np.array([lat, lon, 1.0]) @ transform
        return float(x), float(y)

    return ll2px


def haversine_matrix(coords):
    lat = np.radians(coords[:, 0])[:, None]
    lon = np.radians(coords[:, 1])[:, None]
    dlat, dlon = lat - lat.T, lon - lon.T
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def coordinate_distance_m(first, second):
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = (math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) *
             math.sin(dlon / 2) ** 2)
    return 2 * EARTH_R * math.asin(math.sqrt(min(1.0, value)))


def euclidean_matrix(pts):
    """Euclidean distance matrix from a list of (x, y) pixel pairs."""
    a = np.array(pts, dtype=float)
    diff = a[:, None, :] - a[None, :, :]
    return np.sqrt((diff ** 2).sum(axis=2))


# ----------------------------------------------------------------------------
# 3. TSP: nearest neighbour + 2-opt + Or-opt
# ----------------------------------------------------------------------------

def path_len(order, D):
    return float(D[order[:-1], order[1:]].sum())


def nearest_neighbor(D, start, end=None, nodes=None):
    """Open path start -> all nodes -> end (end omitted if None)."""
    todo = set(nodes)
    order, cur = [start], start
    while todo:
        nxt = min(todo, key=lambda j: D[cur, j])
        order.append(nxt)
        todo.discard(nxt)
        cur = nxt
    if end is not None:
        order.append(end)
    return order


def two_opt(order, D, fixed_ends=True):
    """2-opt + Or-opt (segment move, len 1-3) until no improvement."""
    order = list(order)
    n = len(order)
    improved = True
    while improved:
        improved = False
        o = np.array(order)
        # 2-opt: reverse o[i:j+1]
        lo = 1 if fixed_ends else 0
        for i in range(lo, n - 2):
            a, b = o[i - 1], o[i]
            # vectorized gain for all j
            j = np.arange(i + 1, n - 1)
            c, d = o[j], o[j + 1]
            gain = D[a, b] + D[c, d] - D[a, c] - D[b, d]
            k = int(np.argmax(gain))
            if gain[k] > 1e-9:
                jj = i + 1 + k
                order[i:jj + 1] = order[i:jj + 1][::-1]
                o = np.array(order)
                improved = True
        # Or-opt: move segment of length L to another position
        for L in (1, 2, 3):
            i = 1
            while i < n - 1 - L:
                seg = order[i:i + L]
                a, b, c = order[i - 1], seg[0], seg[-1]
                d = order[i + L]
                rem_gain = D[a, b] + D[c, d] - D[a, d]
                if rem_gain > 1e-9:
                    rest = order[:i] + order[i + L:]
                    best_gain, best_pos = 0.0, None
                    for p in range(1, len(rest)):
                        u, v = rest[p - 1], rest[p]
                        ins_cost = D[u, b] + D[c, v] - D[u, v]
                        if rem_gain - ins_cost > best_gain + 1e-9:
                            best_gain, best_pos = rem_gain - ins_cost, p
                    if best_pos is not None:
                        order = rest[:best_pos] + seg + rest[best_pos:]
                        improved = True
                        continue
                i += 1
    return order


def solve_open(D, start, end, nodes):
    order = nearest_neighbor(D, start, end, nodes)
    order = two_opt(order, D, fixed_ends=True)
    return order, path_len(order, D)


def solve_loop(D, depot, nodes):
    order = nearest_neighbor(D, depot, depot, nodes)
    order = two_opt(order, D, fixed_ends=True)  # endpoints both = depot
    return order, path_len(order, D)


def solve_circle(D, nodes, tries=3):
    """Shortest closed tour over the nodes only — no fixed start/end.
    Runs NN+2-opt from a few different seeds and keeps the best cycle."""
    best = None
    seeds = [nodes[(len(nodes) * k) // tries] for k in range(tries)]
    for s in seeds:
        rest = [n for n in nodes if n != s]
        order = nearest_neighbor(D, s, s, rest)
        order = two_opt(order, D, fixed_ends=True)
        ln = path_len(order, D)
        if best is None or ln < best[1]:
            best = (order, ln)
    return best


# ----------------------------------------------------------------------------
# 4. OSRM walking distances and geometry
# ----------------------------------------------------------------------------

OSRM_BASE = "https://routing.openstreetmap.de/routed-foot"


class StreetRoutingError(RuntimeError):
    """Raised when a street-following route cannot be calculated."""


@dataclass(frozen=True)
class StreetRoute:
    geometry: list[tuple[float, float]]
    distance_m: float
    duration_s: float
    snapped_waypoints: list[tuple[float, float]]
    snap_distances_m: list[float]


def validate_geographic_route(route, expected_waypoints, circular):
    if len(route.snapped_waypoints) != expected_waypoints:
        raise StreetRoutingError(
            "street router returned an incomplete waypoint list")
    if len(route.snap_distances_m) != expected_waypoints:
        raise StreetRoutingError(
            "street router returned incomplete snap distances")
    if len(route.geometry) < 2:
        raise StreetRoutingError("street router returned empty geometry")
    if circular and coordinate_distance_m(
        route.geometry[0], route.geometry[-1]
    ) > 2.0:
        raise StreetRoutingError("circular GPS route is not closed")
    return route


class StreetRouter:
    """Pedestrian distances and geometry from the free FOSSGIS OSRM API."""

    _request_lock = threading.Lock()
    _last_request_at = None

    def __init__(self, base_url=OSRM_BASE, min_interval=1.0,
                 table_block=40, route_chunk=24):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.table_block = table_block
        self.route_chunk = route_chunk

    def _request_json(self, service, coords, query):
        locs = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in coords)
        url = (f"{self.base_url}/{service}/v1/foot/{locs}?"
               f"{urllib.parse.urlencode(query)}")
        req = urllib.request.Request(
            url, headers={"User-Agent": "hoffroute/1.0"})
        router_type = type(self)
        with router_type._request_lock:
            if router_type._last_request_at is not None:
                remaining = self.min_interval - (
                    time.monotonic() - router_type._last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            try:
                with urllib.request.urlopen(
                    req, timeout=30, context=SSL_CTX
                ) as response:
                    return json.loads(response.read())
            except Exception as exc:
                raise StreetRoutingError(
                    f"street router unavailable: {exc}") from exc
            finally:
                router_type._last_request_at = time.monotonic()

    @staticmethod
    def _require_ok(data):
        if data.get("code") != "Ok":
            raise StreetRoutingError(
                data.get("message") or f"street router returned {data.get('code', 'an error')}")

    def distance_matrix(self, coords):
        size = len(coords)
        matrix = np.empty((size, size), dtype=float)
        for source_start in range(0, size, self.table_block):
            source_indices = list(range(
                source_start, min(source_start + self.table_block, size)))
            for destination_start in range(0, size, self.table_block):
                destination_indices = list(range(
                    destination_start,
                    min(destination_start + self.table_block, size)))
                request_indices = source_indices + [
                    i for i in destination_indices if i not in source_indices
                ]
                positions = {
                    original: position
                    for position, original in enumerate(request_indices)
                }
                request_coords = [coords[i] for i in request_indices]
                data = self._request_json("table", request_coords, {
                    "annotations": "distance",
                    "sources": ";".join(
                        str(positions[i]) for i in source_indices),
                    "destinations": ";".join(
                        str(positions[i]) for i in destination_indices),
                })
                self._require_ok(data)
                distances = data.get("distances")
                expected_shape = (len(source_indices), len(destination_indices))
                tile = np.asarray(distances, dtype=object)
                if tile.shape != expected_shape or any(
                    value is None for row in tile for value in row
                ):
                    raise StreetRoutingError(
                        "street router returned an incomplete distance matrix")
                matrix[np.ix_(source_indices, destination_indices)] = \
                    tile.astype(float)
        return matrix

    def route(self, coords):
        geometry, snapped = [], []
        distance, duration = 0.0, 0.0
        index = 0
        while index < len(coords) - 1:
            part = coords[index:index + self.route_chunk + 1]
            data = self._request_json("route", part, {
                "overview": "full", "geometries": "geojson", "steps": "false",
            })
            self._require_ok(data)
            routes = data.get("routes") or []
            if not routes:
                raise StreetRoutingError("street router returned no route")
            route = routes[0]
            legs = route.get("legs") or []
            if len(legs) != len(part) - 1:
                raise StreetRoutingError(
                    "street router returned incomplete route legs")
            segment = [
                (lat, lon) for lon, lat in route["geometry"]["coordinates"]
            ]
            if len(segment) < 2:
                raise StreetRoutingError(
                    "street router returned empty route geometry")
            if geometry and geometry[-1] == segment[0]:
                geometry.extend(segment[1:])
            else:
                geometry.extend(segment)
            waypoints = data.get("waypoints") or []
            if len(waypoints) != len(part):
                raise StreetRoutingError(
                    "street router returned an incomplete waypoint list")
            part_snapped = [
                (float(item["location"][1]), float(item["location"][0]))
                for item in waypoints
            ]
            snapped.extend(part_snapped if not snapped else part_snapped[1:])
            distance += float(route["distance"])
            duration += float(route["duration"])
            index += self.route_chunk
        snap_distances = [
            coordinate_distance_m(original, routed)
            for original, routed in zip(coords, snapped)
        ]
        return StreetRoute(
            geometry=geometry,
            distance_m=distance,
            duration_s=duration,
            snapped_waypoints=snapped,
            snap_distances_m=snap_distances,
        )


# ----------------------------------------------------------------------------
# 5. exports
# ----------------------------------------------------------------------------

def write_gpx(path, name, stops, track=None):
    """stops: [(lat, lon, label), ...] in visiting order."""
    w = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<gpx version="1.1" creator="hoffroute" xmlns="http://www.topografix.com/GPX/1/1">',
         f'  <metadata><name>{name}</name></metadata>']
    for lat, lon, label in stops:
        w.append(f'  <wpt lat="{lat:.6f}" lon="{lon:.6f}"><name>{label}</name></wpt>')
    w.append(f'  <rte><name>{name}</name>')
    for lat, lon, label in stops:
        w.append(f'    <rtept lat="{lat:.6f}" lon="{lon:.6f}"><name>{label}</name></rtept>')
    w.append('  </rte>')
    if track:
        w.append(f'  <trk><name>{name} (walking path)</name><trkseg>')
        for lat, lon in track:
            w.append(f'    <trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>')
        w.append('  </trkseg></trk>')
    w.append('</gpx>')
    Path(path).write_text("\n".join(w), encoding="utf-8")


def write_geojson(path, variants):
    feats = []
    for name, stops, track in variants:
        for i, (lat, lon, label) in enumerate(stops):
            feats.append({"type": "Feature",
                          "properties": {"route": name, "seq": i, "name": label},
                          "geometry": {"type": "Point", "coordinates": [lon, lat]}})
        line = track if track else [(la, lo) for la, lo, _ in stops]
        feats.append({"type": "Feature", "properties": {"route": name, "kind": "path"},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[lo, la] for la, lo in line]}})
    Path(path).write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def write_kml(path, name, stops, track=None):
    """One continuous walking line + stop placemarks. Imports into Google
    My Maps / Google Earth / Organic Maps as a single route."""
    line = track if track else [(la, lo) for la, lo, _ in stops]
    coords = " ".join(f"{lo:.6f},{la:.6f},0" for la, lo in line)
    w = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
         f'<name>{name}</name>',
         '<Style id="route"><LineStyle><color>ff6c33d6</color><width>4</width></LineStyle></Style>',
         '<Style id="stop"><IconStyle><scale>0.6</scale><Icon>'
         '<href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href>'
         '</Icon></IconStyle></Style>',
         f'<Placemark><name>{name}</name><styleUrl>#route</styleUrl>'
         f'<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates>'
         '</LineString></Placemark>',
         '<Folder><name>Stops</name>']
    for lat, lon, label in stops:
        w.append(f'<Placemark><name>{label}</name><styleUrl>#stop</styleUrl>'
                 f'<Point><coordinates>{lon:.6f},{lat:.6f},0</coordinates></Point></Placemark>')
    w += ['</Folder>', '</Document></kml>']
    Path(path).write_text("\n".join(w), encoding="utf-8")


def gmaps_overview_link(stops, max_wp=9):
    """ONE Google Maps walking link for the whole route, downsampled to the
    9-waypoint URL limit. Follows the route shape end to end; the PDF/GPX
    carry the exact stop-by-stop order."""
    pts = [(la, lo) for la, lo, _ in stops]
    origin, dest = pts[0], pts[-1]
    inner = pts[1:-1]
    if len(inner) > max_wp:
        idx = np.linspace(0, len(inner) - 1, max_wp).round().astype(int)
        inner = [inner[i] for i in idx]
    q = {"api": "1", "travelmode": "walking",
         "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
         "destination": f"{dest[0]:.6f},{dest[1]:.6f}"}
    if inner:
        q["waypoints"] = "|".join(f"{la:.6f},{lo:.6f}" for la, lo in inner)
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(q)


def gmaps_links(stops, max_wp=9):
    """Google Maps allows origin + destination + 9 waypoints per link."""
    links, i = [], 0
    pts = [(la, lo) for la, lo, _ in stops]
    while i < len(pts) - 1:
        part = pts[i:i + max_wp + 2]
        origin, dest, mid = part[0], part[-1], part[1:-1]
        q = {"api": "1", "travelmode": "walking",
             "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
             "destination": f"{dest[0]:.6f},{dest[1]:.6f}"}
        if mid:
            q["waypoints"] = "|".join(f"{la:.6f},{lo:.6f}" for la, lo in mid)
        links.append("https://www.google.com/maps/dir/?" + urllib.parse.urlencode(q))
        i += len(part) - 1
    return links


def write_html(path, title, variants, stations):
    layers = []
    for vi, (name, stops, track) in enumerate(variants):
        color = ["#d6336c", "#1c7ed6", "#2b8a3e"][vi % 3]
        line = track if track else [(la, lo) for la, lo, _ in stops]
        markers = "".join(
            f'L.circleMarker([{la:.6f},{lo:.6f}],{{radius:5,color:"{color}",fillColor:"#fff",'
            f'fillOpacity:1,weight:2}}).bindTooltip("{label}").addTo(g{vi});'
            for la, lo, label in stops)
        coords = ",".join(f"[{la:.6f},{lo:.6f}]" for la, lo in line)
        layers.append(
            f'var g{vi}=L.layerGroup();'
            f'L.polyline([{coords}],{{color:"{color}",weight:4,opacity:.75}}).addTo(g{vi});'
            f'{markers}')
    st = "".join(
        f'L.marker([{s["lat"]:.6f},{s["lon"]:.6f}]).bindTooltip("{s["name"]}").addTo(map);'
        for s in stations)
    overlays = ",".join(f'"{v[0]}":g{i}' for i, v in enumerate(variants))
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{{height:100%;margin:0}}</style></head><body><div id="map"></div>
<script>
var map=L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
{''.join(layers)}
g0.addTo(map);
L.control.layers(null,{{{overlays}}},{{collapsed:false}}).addTo(map);
{st}
map.fitBounds(g0.getLayers()[0].getBounds().pad(0.08));
</script></body></html>"""
    Path(path).write_text(html, encoding="utf-8")


def station_labels_for_icons(icons, stations, route_order=None):
    """Return named callouts and warnings; never invent a station name."""
    named = [((station["px"], station["py"]), index, station["name"])
             for index, station in enumerate(stations)]
    labels, warnings = [], []
    route_start = route_order[0] if route_order else None
    route_end = route_order[-1] if route_order else None
    for kind, x, y in icons:
        best_name, best_index, best_dist = None, None, float("inf")
        for (sx, sy), station_index, station_name in named:
            distance = math.hypot(x - sx, y - sy)
            if distance < best_dist:
                best_dist = distance
                best_name = station_name
                best_index = station_index
        if best_dist >= 30 or best_name is None:
            warnings.append(
                f"Station name unavailable for {kind} at ({x:.0f}, {y:.0f})")
            continue
        if best_index == route_start == route_end:
            role = "start_end"
        elif best_index == route_start:
            role = "start"
        elif best_index == route_end:
            role = "end"
        else:
            role = None
        labels.append({
            "x": float(x),
            "y": float(y),
            "name": str(best_name),
            "kind": kind[0],
            "role": role,
        })
    return labels, warnings


def annotate_pdf(src_doc_path, out_path, order_px, title, color=(0.83, 0.07, 0.41),
                 dpi=300, station_labels=None, route_px=None,
                 access_spurs=None):
    """Draw the route polyline + stop numbers onto page 0 of the PDF."""
    s = 72.0 / dpi  # px -> pdf points
    doc = fitz.open(src_doc_path)
    page = doc[0]
    pts = [fitz.Point(x * s, y * s) for x, y in order_px]
    route_pts = [fitz.Point(x * s, y * s)
                 for x, y in (route_px or order_px)]

    closed = order_px[0] == order_px[-1]
    if access_spurs:
        spur_shape = page.new_shape()
        for (ax, ay), (bx, by) in access_spurs:
            spur_shape.draw_line(
                fitz.Point(ax * s, ay * s), fitz.Point(bx * s, by * s))
        spur_shape.finish(color=(0.11, 0.46, 0.84), width=1.1,
                          dashes="[2 2]", stroke_opacity=0.65)
        spur_shape.commit()

    shape = page.new_shape()
    shape.draw_polyline(route_pts)
    shape.finish(color=(0.11, 0.46, 0.84), width=2.2, lineJoin=1, lineCap=1,
                 stroke_opacity=0.8)
    shape.commit()

    # Flag markers: vertical pole with a filled triangle pennant at the top.
    # Green flag = start, red flag = end (omitted for closed circular tours).
    ph, fw, fh = 13, 9, 6  # pole height, flag width, flag height (PDF points)

    def draw_flag(pt, fill):
        top = fitz.Point(pt.x, pt.y - ph)
        mid = fitz.Point(pt.x + fw, pt.y - ph + fh / 2)
        bot = fitz.Point(pt.x, pt.y - ph + fh)
        sh = page.new_shape()
        sh.draw_line(pt, top)
        sh.finish(color=(0.15, 0.15, 0.15), width=1.2)
        sh.draw_polyline([top, mid, bot, top])
        sh.finish(fill=fill, color=fill, width=0.3)
        sh.commit()

    draw_flag(pts[0], (0.13, 0.55, 0.13))
    if not closed:
        draw_flag(pts[-1], (0.80, 0.10, 0.10))

    # Explicit station labels. The flyer often prints only U/S icons, so draw
    # the resolved station names onto the annotated output.
    if station_labels:
        for station in station_labels:
            if isinstance(station, dict):
                x, y = station["x"], station["y"]
                name = str(station["name"])
                kind = str(station.get("kind", "")).upper()
                role = station.get("role")
            else:
                x, y, name = station
                kind, role = "", None
            p = fitz.Point(x * s, y * s)
            transit = f"{kind}-BAHN" if kind in {"S", "U"} else "STATION"
            role_text = {
                "start": "START",
                "end": "ZIEL",
                "start_end": "START / ZIEL",
            }.get(role)
            header = f"{role_text} · {transit}" if role_text else transit
            accent = {
                "start": (0.13, 0.55, 0.13),
                "end": (0.80, 0.10, 0.10),
                "start_end": (0.16, 0.45, 0.75),
            }.get(role, (0.16, 0.45, 0.75))
            name_size = 8.0
            while (fitz.get_text_length(name, fontname="hebo", fontsize=name_size)
                   > 126 and name_size > 6.5):
                name_size -= 0.5
            header_size = 5.8
            pad_x = 4.0
            width = min(136, max(
                76,
                fitz.get_text_length(name, fontname="hebo", fontsize=name_size)
                + 2 * pad_x,
                fitz.get_text_length(header, fontname="hebo", fontsize=header_size)
                + 2 * pad_x,
            ))
            height = 27.0
            if p.x + 8 + width <= page.rect.width - 4:
                left = p.x + 8
            else:
                left = max(4, p.x - width - 8)
            top = min(max(p.y - height / 2, 4), page.rect.height - height - 20)
            rect = fitz.Rect(left, top, left + width, top + height)
            sh = page.new_shape()
            edge = fitz.Point(rect.x0 if left > p.x else rect.x1,
                              rect.y0 + height / 2)
            sh.draw_line(p, edge)
            sh.finish(color=accent, width=1.1)
            sh.draw_rect(rect)
            sh.finish(color=accent, fill=(1, 1, 1),
                      width=1.0, fill_opacity=0.94, stroke_opacity=1)
            sh.commit()
            page.insert_text(
                fitz.Point(rect.x0 + pad_x, rect.y0 + 8), header,
                fontsize=header_size, fontname="hebo", color=accent)
            page.insert_text(
                fitz.Point(rect.x0 + pad_x, rect.y0 + 20), name,
                fontsize=name_size, fontname="hebo",
                color=(0.05, 0.05, 0.05))

    # stop numbers (skip start/end stations)
    for i, p in enumerate(pts[1:-1], start=1):
        page.insert_text(p + (4.2, -3.5), str(i), fontsize=4.3,
                         color=(0.05, 0.25, 0.55),
                         render_mode=0)
    # title banner — full-width white strip at the bottom so the title never
    # overlaps existing footer text regardless of what the PDF contains there.
    _ty = page.rect.height - 5
    _sh = page.new_shape()
    _sh.draw_rect(fitz.Rect(0, _ty - 13, page.rect.width, page.rect.height))
    _sh.finish(fill=(1, 1, 1), color=(1, 1, 1), width=0,
               fill_opacity=1.0, stroke_opacity=0)
    _sh.commit()
    page.insert_text(fitz.Point(5, _ty), title,
                     fontsize=9, color=color, render_mode=0)
    doc.save(out_path)
    doc.close()


# ----------------------------------------------------------------------------
# pipeline
# ----------------------------------------------------------------------------

def unpack_station_resolution(result):
    """Accept the structured resolver result while keeping list compatibility."""
    if hasattr(result, "stations") and hasattr(result, "warnings"):
        return list(result.stations), list(result.warnings)
    return list(result or []), []

def fetch_pdf(src, dest_dir):
    """Accept a local path or an http(s) URL. URLs are downloaded into
    dest_dir; returns the local Path either way."""
    if not str(src).lower().startswith(("http://", "https://")):
        return Path(src)
    name = Path(urllib.parse.urlparse(str(src)).path).name or "map"
    dest = Path(dest_dir) / name
    if dest.suffix.lower() != ".pdf":
        dest = dest.with_suffix(".pdf")
    req = urllib.request.Request(str(src), headers={"User-Agent": "hoffroute/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{src} did not return a PDF document")
    dest.write_bytes(data)
    return dest


def run_pipeline(pdf_path, calib, out_dir, dpi=300, start=None, end=None,
                 log=print, resolve_stations=None, street_router=None):
    """Build a printed-street PDF route and optional geographic exports."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    calibrated = (calib is not None and
                  len(calib.get("control_points", [])) >= 3)
    steps = 6
    router = (street_router or StreetRouter()) if calibrated else None

    # --- 1. render + detect dots ---
    log(f"1/{steps} rendering + detecting dots ...")
    _, img = render_page(pdf_path, dpi)
    h, w = img.shape[:2]
    bbox = (calib.get("map_bbox_px", [0, 0, w, h])
            if calib else [0, 0, w, h])
    dots_px = detect_dots(img, bbox)
    log(f"    {len(dots_px)} market dots found")
    if not dots_px:
        raise ValueError("no market dots detected — check map_bbox_px")

    # If calibrated but station names are missing, try to resolve them now.
    if calibrated and not calib.get("stations") and resolve_stations:
        _icons = detect_station_icons(img)
        if _icons:
            log("    resolving station names from transit data ...")
            try:
                _result = resolve_stations(
                    _icons, calib["control_points"], calib.get("context"))
                _resolved, _station_warnings = unpack_station_resolution(
                    _result)
                calib = dict(
                    calib,
                    station_warnings=(
                        list(calib.get("station_warnings", [])) +
                        _station_warnings
                    ),
                )
                if _resolved:
                    calib = dict(calib, stations=_resolved)
                    log(f"    {len(_resolved)} station name(s) resolved")
                for _warning in _station_warnings:
                    log(f"    {_warning}")
            except Exception as _e:
                log(f"    station name resolution failed: {_e}")

    # --- 2. printed-street graph + optional georeferencing ---
    log(f"2/{steps} extracting printed street network ...")
    graph = FlyerStreetGraph.from_image(img, bbox, dpi=dpi)
    max_access_px = 90.0 * dpi / 300.0
    dot_access = graph.snap_stops(dots_px, max_access_px)

    stations = list(calib.get("stations", [])) if calibrated else []
    station_access, connected_stations = [], []
    for station in stations:
        try:
            access = graph.snap_stops(
                [(station["px"], station["py"])], max_access_px)[0]
            if dot_access and not np.isfinite(
                graph.distance_matrix([access, dot_access[0]])[0, 1]
            ):
                raise FlyerStreetError("disconnected station access")
        except FlyerStreetError as exc:
            log(f"    skipping station {station['name']}: {exc}")
            continue
        connected_stations.append(station)
        station_access.append(access)
    stations = connected_stations
    all_access = station_access + dot_access
    D = graph.distance_matrix(all_access)
    ns = len(stations)
    dot_idx = list(range(ns, ns + len(dots_px)))
    station_names = [station["name"] for station in stations]
    node_to_px = {
        index: access.stop_xy for index, access in enumerate(all_access)
    }

    rms = None
    coords_arr = None
    straight_D = D
    if calibrated:
        log("    georeferencing GPS export coordinates ...")
        px2ll, rms = fit_affine(calib["control_points"])
        log(f"    affine fit over {len(calib['control_points'])} control points "
            f"(rms {rms:.0f} m)")
        dots_ll = [px2ll(x, y) for x, y in dots_px]
        coords_arr = np.array([[s["lat"], s["lon"]] for s in stations] +
                               [list(d) for d in dots_ll]).reshape(-1, 2)
        straight_D = haversine_matrix(coords_arr)

    # --- 3. solve TSP variants ---
    log(f"3/{steps} solving routes ...")
    variants = []

    # variant A: open path between two station nodes (best pair, or forced)
    if ns >= 2 or (start and end and calibrated):
        if start and end and calibrated:
            pairs = [(station_names.index(start), station_names.index(end))]
        else:
            min_gap = 10
            pairs = [(i, j) for i in range(ns) for j in range(i + 1, ns)
                     if D[i, j] > min_gap]
            pairs = pairs or [(0, ns - 1)]
        bestA = None
        for i, j in pairs:
            o, ln = solve_open(D, i, j, dot_idx)
            if bestA is None or ln < bestA[1]:
                bestA = (o, ln, i, j)
        orderA, lenA, sA, eA = bestA
        variants.append(dict(
            key="station_to_station", order=orderA, bird=lenA,
            name=f"Hofflohmaerkte {station_names[sA]} to {station_names[eA]}",
            title=f"Route: {station_names[sA]} (S) -> {station_names[eA]} (Z)"))
        log(f"    A  {station_names[sA]} -> {station_names[eA]}: "
            f"{lenA:.0f} street px")

    # variant B: closed loop from/to one station node
    if ns >= 1:
        depot = (station_names.index(start)
                 if start and calibrated and start in station_names
                 else variants[0]["order"][0] if variants else 0)
        orderB, lenB = solve_loop(D, depot, dot_idx)
        variants.append(dict(
            key="loop", order=orderB, bird=lenB,
            name=f"Hofflohmaerkte loop from {station_names[depot]}",
            title=f"Rundweg ab/bis {station_names[depot]}"))
        log(f"    B  loop from {station_names[depot]}: {lenB:.0f} street px")

    # variant C: shortest free circle over dots only, no fixed start/end
    orderC, lenC = solve_circle(D, dot_idx)
    variants.append(dict(
        key="circle", order=orderC, bird=lenC,
        name="Hofflohmaerkte circular tour (dots only)",
        title="Rundtour ueber alle Hoefe (freier Start)"))
    log(f"    C  free circle: {lenC:.0f} street px")

    for variant in variants:
        variant["graph_px"] = path_len(variant["order"], D)
        variant["bird"] = path_len(variant["order"], straight_D)
        variant["route_px"] = graph.route_geometry(
            variant["order"], all_access)
        validation = graph.validate_closed_route(
            variant["order"], all_access, variant["route_px"])
        expected_closed = variant["key"] in {"loop", "circle"}
        if expected_closed and not validation.closed:
            raise FlyerStreetError(
                f"{variant['key']} route did not return to its start")
        if (variant["key"] == "circle" and
                validation.unique_stop_count != len(dots_px)):
            raise FlyerStreetError(
                "circular route does not contain every market marker")
        variant["validation"] = validation
        unique_order = (variant["order"][:-1]
                        if validation.closed else variant["order"])
        unique_order = list(dict.fromkeys(unique_order))
        variant["access_spurs"] = [
            (all_access[index].stop_xy, all_access[index].street_xy)
            for index in unique_order
        ]

    # --- 4-5. GPS exports (calibrated mode only) ---
    gps_available = calibrated
    gps_warning = None
    if calibrated:
        def stops_of(order):
            closed = order[0] == order[-1]
            res = []
            for k, n in enumerate(order):
                if n < ns:
                    label = station_names[n]
                elif closed and k == len(order) - 1:
                    label = "Back at start"
                else:
                    label = f"Stop {k}"
                res.append((coords_arr[n, 0], coords_arr[n, 1], label))
            return res

        for v in variants:
            v["stops"] = stops_of(v["order"])

        try:
            log(f"4/{steps} fetching walking geometry (OSRM) ...")
            for v in variants:
                route_coords = [
                    (la, lo) for la, lo, _ in v["stops"]
                ]
                street_route = validate_geographic_route(
                    router.route(route_coords),
                    expected_waypoints=len(route_coords),
                    circular=v["order"][0] == v["order"][-1],
                )
                v["track"] = street_route.geometry
                v["dist"] = street_route.distance_m
                v["dur"] = street_route.duration_s
                v["snap_distances_m"] = street_route.snap_distances_m
                log(f"    {v['key']}: {street_route.distance_m/1000:.2f} km "
                    f"on streets (~{street_route.duration_s/3600:.1f} h pure "
                    "walking)")
        except StreetRoutingError as exc:
            gps_available = False
            gps_warning = f"GPS route unavailable: {exc}"
            log(f"    {gps_warning}")

        if gps_available:
            log(f"5/{steps} writing GPS exports ...")
            triples = [
                (v["name"], v["stops"], v["track"]) for v in variants
            ]
            for v in variants:
                write_gpx(out / f"route_{v['key']}.gpx", v["name"],
                          v["stops"], v["track"])
                write_kml(out / f"route_{v['key']}.kml", v["name"],
                          v["stops"], v["track"])
                v["gmaps"] = gmaps_overview_link(v["stops"])
            write_geojson(out / "routes.geojson", triples)
            write_html(out / "routes_map.html", "Hofflohmaerkte routes",
                       triples, stations)
            txt = [
                "# Google Maps - one walking link per route (whole route in one",
                "# shot, downsampled to Google's 9-waypoint URL limit; import the",
                "# .kml into Google My Maps for the exact full line).", "",
            ]
            for v in variants:
                txt += [f"## {v['name']}", v["gmaps"], ""]
            txt += [
                "# Appendix: exact stop-by-stop legs (9 waypoints per link)", ""
            ]
            for v in variants:
                links = gmaps_links(v["stops"])
                txt.append(f"## {v['name']} ({len(links)} legs)")
                txt += [f"{k + 1}. {url}" for k, url in enumerate(links)]
                txt.append("")
            (out / "google_maps_links.txt").write_text("\n".join(txt))
    else:
        gps_warning = "GPS route unavailable: no reliable map calibration"

    # --- last step: annotate PDFs + previews (both modes) ---
    log(f"{steps}/{steps} annotating PDFs + previews ...")
    fitz.open(pdf_path)[0].get_pixmap(dpi=110).save(out / "original.png")
    station_warnings = list(calib.get("station_warnings", [])) if calib else []
    for v in variants:
        order_px = [node_to_px[n] for n in v["order"]]
        if gps_available and v.get("dist") is not None:
            km = f"{v['dist'] / 1000:.1f} km"
            title_str = f"{v['title']} | {len(dots_px)} Hoefe | ~{km}"
        else:
            title_str = f"{v['title']} | {len(dots_px)} Hoefe"
        pdf_out = out / f"route_{v['key']}.pdf"
        station_labels, label_warnings = station_labels_for_icons(
            detect_station_icons(img), stations, v["order"])
        for warning in label_warnings:
            if warning not in station_warnings:
                station_warnings.append(warning)
        station_labels = station_labels or None
        annotate_pdf(pdf_path, pdf_out, order_px, title_str, dpi=dpi,
                     station_labels=station_labels, route_px=v["route_px"],
                     access_spurs=v["access_spurs"])
        fitz.open(pdf_out)[0].get_pixmap(dpi=110).save(
            out / f"route_{v['key']}.png")

    return {
        "dots": len(dots_px),
        "fit_rms_m": round(rms, 1) if rms is not None else None,
        "station_warnings": station_warnings,
        "gps_available": gps_available,
        "gps_warning": gps_warning,
        "flyer_route": {
            "available": True,
            "warning": None,
        },
        "gps_route": {
            "available": gps_available,
            "warning": gps_warning,
        },
        "variants": [{
            "key": v["key"], "name": v["name"],
            "bird_km": round(v["bird"] / 1000, 2) if calibrated else None,
            "graph_px": round(v["graph_px"], 1),
            "street_km": round(v["dist"] / 1000, 2)
                         if gps_available and v.get("dist") else None,
            "walk_h": round(v["dur"] / 3600, 1)
                      if gps_available and v.get("dur") else None,
            "max_snap_distance_m": round(max(v.get("snap_distances_m", [0])), 1)
                                   if gps_available else None,
            "mean_snap_distance_m": round(
                sum(v.get("snap_distances_m", [0])) /
                len(v.get("snap_distances_m", [0])), 1)
                                   if gps_available else None,
            "closed": v["validation"].closed,
            "unique_stops": v["validation"].unique_stop_count,
            "access_spurs": len(v["access_spurs"]),
            "max_access_spur_px": round(
                v["validation"].max_access_distance_px, 1),
            "max_road_offset_px": round(
                v["validation"].max_mask_distance_px, 1),
            "pdf": f"route_{v['key']}.pdf",
            "gpx": f"route_{v['key']}.gpx" if gps_available else None,
            "kml": f"route_{v['key']}.kml" if gps_available else None,
            "png": f"route_{v['key']}.png",
            "gmaps": v.get("gmaps") if gps_available else None,
        } for v in variants],
        "files": sorted(f.name for f in out.iterdir() if f.is_file()),
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("pdf", help="path or http(s) URL of the map PDF")
    ap.add_argument("--calib", help="calibration JSON")
    ap.add_argument("-o", "--out", default="route_out")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--start", help="force start station name")
    ap.add_argument("--end", help="force end station name")
    ap.add_argument("--find-landmarks", action="store_true",
                    help="calibration helper: detect U/S station icons, write "
                         "calib_template.json + map_render.png, then exit")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = fetch_pdf(args.pdf, out)
    if str(pdf_path) != str(args.pdf):
        print(f"downloaded {args.pdf} -> {pdf_path}")

    if args.find_landmarks:
        find_landmarks(pdf_path, out, args.dpi)
        return
    calib = (json.loads(Path(args.calib).read_text())
             if args.calib else None)

    run_pipeline(pdf_path, calib, out, dpi=args.dpi, start=args.start,
                 end=args.end)
    print(f"\nDone -> {out}/")
    for f in sorted(out.iterdir()):
        print("   ", f.name)


if __name__ == "__main__":
    main()
