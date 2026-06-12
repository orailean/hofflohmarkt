#!/usr/bin/env python3
"""
hoffroute.py - Plan the shortest walking route over all market dots in a
Hofflohmaerkte map PDF.

Pipeline:
  1. Render the PDF page and detect the red/pink market dots (color threshold
     + distance-transform peak splitting for touching dots).
  2. Georeference pixel coords -> WGS84 with an affine fit over control points
     from a calibration JSON (e.g. U/S-Bahn station icons).
  3. Solve the TSP for up to three variants:
       A. open path between two stations (best pair, or --start/--end)
       B. closed loop from/to one station
       C. shortest free circle over the dots only (no fixed start/end;
          always produced, and the only variant if no stations are given)
  4. Optionally fetch the real walking geometry/distance from the public
     FOSSGIS OSRM foot router.
  5. Export: GPX (waypoints + route + track), GeoJSON, chunked Google Maps
     links, a self-contained Leaflet HTML map, and the original PDF with the
     route drawn on top (one annotated PDF per variant).

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
import json
import math
import ssl
import sys
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


def detect_dots(img, bbox, min_radius_px=6, merge_dist_px=18):
    """Detect pink/red market dots inside bbox; split touching clusters."""
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


def haversine_matrix(coords):
    lat = np.radians(coords[:, 0])[:, None]
    lon = np.radians(coords[:, 1])[:, None]
    dlat, dlon = lat - lat.T, lon - lon.T
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


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
# 4. OSRM walking geometry (optional)
# ----------------------------------------------------------------------------

OSRM_BASE = "https://routing.openstreetmap.de/routed-foot/route/v1/foot/"


def osrm_geometry(coords, chunk=24):
    """Walking geometry along ordered coords [(lat,lon),...]. Returns
    (list[(lat,lon)] polyline, meters, seconds) or (None, None, None)."""
    geom, dist, dur = [], 0.0, 0.0
    try:
        i = 0
        while i < len(coords) - 1:
            part = coords[i:i + chunk + 1]
            locs = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in part)
            url = OSRM_BASE + locs + "?overview=full&geometries=geojson&steps=false"
            req = urllib.request.Request(url, headers={"User-Agent": "hoffroute/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                data = json.loads(r.read())
            if data.get("code") != "Ok":
                return None, None, None
            route = data["routes"][0]
            dist += route["distance"]
            dur += route["duration"]
            seg = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
            geom.extend(seg if not geom else seg[1:])
            i += chunk
        return geom, dist, dur
    except Exception as e:
        print(f"  OSRM unavailable ({e}); falling back to straight lines", file=sys.stderr)
        return None, None, None


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


def annotate_pdf(src_doc_path, out_path, order_px, title, color=(0.83, 0.07, 0.41),
                 dpi=300):
    """Draw the route polyline + stop numbers onto page 0 of the PDF."""
    s = 72.0 / dpi  # px -> pdf points
    doc = fitz.open(src_doc_path)
    page = doc[0]
    pts = [fitz.Point(x * s, y * s) for x, y in order_px]

    closed = order_px[0] == order_px[-1]
    shape = page.new_shape()
    shape.draw_polyline(pts)
    shape.finish(color=(0.11, 0.46, 0.84), width=2.2, lineJoin=1, lineCap=1,
                 stroke_opacity=0.8)
    # start / end markers (a closed tour gets a single start marker)
    shape.draw_circle(pts[0], 7)
    shape.finish(color=(1, 1, 1), fill=(0.13, 0.55, 0.13), width=1.5)
    if not closed:
        shape.draw_circle(pts[-1], 7)
        shape.finish(color=(1, 1, 1), fill=(0.80, 0.10, 0.10), width=1.5)
    shape.commit()

    page.insert_text(pts[0] + (-4, 2.6), "S", fontsize=8, color=(1, 1, 1))
    if not closed:
        page.insert_text(pts[-1] + (-4, 2.6), "Z", fontsize=8, color=(1, 1, 1))

    # stop numbers (skip start/end stations)
    for i, p in enumerate(pts[1:-1], start=1):
        page.insert_text(p + (4.2, -3.5), str(i), fontsize=4.3,
                         color=(0.05, 0.25, 0.55),
                         render_mode=0)
    # title banner
    page.insert_text(fitz.Point(30, page.rect.height - 14), title,
                     fontsize=9, color=color)
    doc.save(out_path)
    doc.close()


# ----------------------------------------------------------------------------
# pipeline
# ----------------------------------------------------------------------------

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
                 use_osrm=True, log=print):
    """Full pipeline on a local PDF. calib is the parsed calibration dict.
    Returns a summary dict (dot count, fit rms, per-variant stats, files)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stations = calib.get("stations", [])

    log("1/6 rendering + detecting dots ...")
    _, img = render_page(pdf_path, dpi)
    dots_px = detect_dots(img, calib["map_bbox_px"])
    log(f"    {len(dots_px)} market dots found")
    if not dots_px:
        raise ValueError("no market dots detected - check map_bbox_px")

    log("2/6 georeferencing ...")
    px2ll, rms = fit_affine(calib["control_points"])
    log(f"    affine fit over {len(calib['control_points'])} control points "
        f"(rms {rms:.0f} m)")
    dots_ll = [px2ll(x, y) for x, y in dots_px]

    # node table: stations first, then dots
    coords = np.array([[s["lat"], s["lon"]] for s in stations] +
                      [list(d) for d in dots_ll]).reshape(-1, 2)
    D = haversine_matrix(coords)
    ns = len(stations)
    dot_idx = list(range(ns, ns + len(dots_ll)))

    log("3/6 solving routes ...")
    names = [s["name"] for s in stations]
    variants = []  # dicts: key, gpx name, pdf title, order, bird-line length

    # variant A: open path between two stations (best pair, or forced)
    if ns >= 2 or (start and end):
        if start and end:
            pairs = [(names.index(start), names.index(end))]
        else:
            pairs = [(i, j) for i in range(ns) for j in range(i + 1, ns)
                     if D[i, j] > 150]  # skip same-place icons (Ostbahnhof U+S)
            pairs = pairs or [(0, ns - 1)]
        bestA = None
        for i, j in pairs:
            o, ln = solve_open(D, i, j, dot_idx)
            if bestA is None or ln < bestA[1]:
                bestA = (o, ln, i, j)
        orderA, lenA, sA, eA = bestA
        variants.append(dict(
            key="station_to_station", order=orderA, bird=lenA,
            name=f"Hofflohmaerkte {names[sA]} to {names[eA]}",
            title=f"Route: {names[sA]} (S) -> {names[eA]} (Z)"))
        log(f"    A  {names[sA]} -> {names[eA]}: {lenA/1000:.2f} km bird-line")

    # variant B: loop from/to one station
    if ns >= 1:
        depot = (names.index(start) if start
                 else variants[0]["order"][0] if variants else 0)
        orderB, lenB = solve_loop(D, depot, dot_idx)
        variants.append(dict(
            key="loop", order=orderB, bird=lenB,
            name=f"Hofflohmaerkte loop from {names[depot]}",
            title=f"Rundweg ab/bis {names[depot]}"))
        log(f"    B  loop from {names[depot]}: {lenB/1000:.2f} km bird-line")

    # variant C: shortest free circle over the dots only, no fixed start/end
    orderC, lenC = solve_circle(D, dot_idx)
    variants.append(dict(
        key="circle", order=orderC, bird=lenC,
        name="Hofflohmaerkte circular tour (dots only)",
        title="Rundtour ueber alle Hoefe (freier Start)"))
    log(f"    C  free circle over all dots: {lenC/1000:.2f} km bird-line")

    def stops_of(order):
        res = []
        closed = order[0] == order[-1]
        for k, n in enumerate(order):
            if n < ns:
                label = names[n]
            elif closed and k == len(order) - 1:
                label = "Back at start"
            else:
                label = f"Stop {k}"
            res.append((coords[n, 0], coords[n, 1], label))
        return res

    for v in variants:
        v["stops"] = stops_of(v["order"])

    log("4/6 fetching walking geometry (OSRM) ...")
    for v in variants:
        v["track"] = v["dist"] = v["dur"] = None
        if use_osrm:
            track, dist, dur = osrm_geometry([(la, lo) for la, lo, _ in v["stops"]])
            v["track"], v["dist"], v["dur"] = track, dist, dur
            if dist:
                log(f"    {v['key']}: {dist/1000:.2f} km on streets "
                    f"(~{dur/3600:.1f} h pure walking)")

    log("5/6 writing exports ...")
    triples = [(v["name"], v["stops"], v["track"]) for v in variants]
    for v in variants:
        write_gpx(out / f"route_{v['key']}.gpx", v["name"], v["stops"], v["track"])
        write_kml(out / f"route_{v['key']}.kml", v["name"], v["stops"], v["track"])
        v["gmaps"] = gmaps_overview_link(v["stops"])
    write_geojson(out / "routes.geojson", triples)
    write_html(out / "routes_map.html", "Hofflohmaerkte routes", triples, stations)
    txt = ["# Google Maps - one walking link per route (whole route in one",
           "# shot, downsampled to Google's 9-waypoint URL limit; import the",
           "# .kml into Google My Maps for the exact full line).", ""]
    for v in variants:
        txt += [f"## {v['name']}", v["gmaps"], ""]
    txt += ["# Appendix: exact stop-by-stop legs (9 waypoints per link)", ""]
    for v in variants:
        links = gmaps_links(v["stops"])
        txt.append(f"## {v['name']} ({len(links)} legs)")
        txt += [f"{k+1}. {u}" for k, u in enumerate(links)]
        txt.append("")
    (out / "google_maps_links.txt").write_text("\n".join(txt))

    log("6/6 annotating PDFs + previews ...")
    # map route node -> pixel position (stations use their icon px)
    st_px = {i: (s["px"], s["py"]) for i, s in enumerate(stations)}
    fitz.open(pdf_path)[0].get_pixmap(dpi=110).save(out / "original.png")
    for v in variants:
        order_px = [st_px[n] if n < ns else dots_px[n - ns] for n in v["order"]]
        km = f"{(v['dist'] or v['bird'])/1000:.1f} km"
        pdf_out = out / f"route_{v['key']}.pdf"
        annotate_pdf(pdf_path, pdf_out, order_px,
                     f"{v['title']} | {len(dots_px)} Hoefe | ~{km}", dpi=dpi)
        fitz.open(pdf_out)[0].get_pixmap(dpi=110).save(out / f"route_{v['key']}.png")

    return {
        "dots": len(dots_px),
        "fit_rms_m": round(rms, 1),
        "variants": [{
            "key": v["key"], "name": v["name"],
            "bird_km": round(v["bird"] / 1000, 2),
            "street_km": round(v["dist"] / 1000, 2) if v["dist"] else None,
            "walk_h": round(v["dur"] / 3600, 1) if v["dur"] else None,
            "pdf": f"route_{v['key']}.pdf", "gpx": f"route_{v['key']}.gpx",
            "kml": f"route_{v['key']}.kml", "png": f"route_{v['key']}.png",
            "gmaps": v["gmaps"],
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
    ap.add_argument("--no-osrm", action="store_true",
                    help="skip online walking-geometry lookup")
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
    if not args.calib:
        ap.error("--calib is required (run --find-landmarks first to create one)")
    calib = json.loads(Path(args.calib).read_text())

    run_pipeline(pdf_path, calib, out, dpi=args.dpi, start=args.start,
                 end=args.end, use_osrm=not args.no_osrm)
    print(f"\nDone -> {out}/")
    for f in sorted(out.iterdir()):
        print("   ", f.name)


if __name__ == "__main__":
    main()
