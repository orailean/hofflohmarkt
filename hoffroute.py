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

        def looks_like_legend_marker(p):
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
            return text_like_components >= 4 and text_like_pixels >= 80

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


def haversine_matrix(coords):
    lat = np.radians(coords[:, 0])[:, None]
    lon = np.radians(coords[:, 1])[:, None]
    dlat, dlon = lat - lat.T, lon - lon.T
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


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
                 dpi=300, station_labels=None):
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
        for x, y, label in station_labels:
            p = fitz.Point(x * s, y * s)
            text = str(label)
            font_size = max(4.2, min(5.1, 108 / max(len(text), 1)))
            pad_x, pad_y = 1.8, 1.1
            w = min(128, max(30, len(text) * font_size * 0.48))
            h = font_size + 2 * pad_y
            if p.x + 7 + w <= page.rect.width - 2:
                left = p.x + 5.5
            else:
                left = max(2, p.x - w - 5.5)
            top = min(max(p.y - h - 4, 2), page.rect.height - h - 2)
            rect = fitz.Rect(left, top, left + w, top + h)
            sh = page.new_shape()
            sh.draw_rect(rect)
            sh.finish(color=(1, 1, 1), fill=(1, 1, 1),
                      width=0.1, fill_opacity=0.72, stroke_opacity=0)
            sh.commit()
            page.insert_text(
                fitz.Point(rect.x0 + pad_x, rect.y0 + pad_y + font_size),
                text,
                fontsize=font_size,
                color=(0.05, 0.05, 0.05),
                render_mode=0)

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
                 use_osrm=True, log=print, resolve_stations=None):
    """Full pipeline on a local PDF. calib is the parsed calibration dict, or
    None to run in pixel-only mode (annotated PDFs only; no GPS exports).
    resolve_stations: optional callable(icons, control_points) -> station list,
    used to look up transit station names when calib has no stations.
    Returns a summary dict (dot count, fit rms, per-variant stats, files)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    calibrated = (calib is not None and
                  len(calib.get("control_points", [])) >= 3)
    steps = 6 if calibrated else 4

    # --- 1. render + detect dots ---
    log(f"1/{steps} rendering + detecting dots ...")
    _, img = render_page(pdf_path, dpi)
    h, w = img.shape[:2]
    bbox = calib["map_bbox_px"] if calibrated else [0, 0, w, h]
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
                _resolved = resolve_stations(_icons, calib["control_points"])
                if _resolved:
                    calib = dict(calib, stations=_resolved)
                    log(f"    {len(_resolved)} station name(s) resolved")
            except Exception as _e:
                log(f"    station name resolution failed: {_e}")

    # --- 2. node table + distance matrix ---
    if calibrated:
        log(f"2/{steps} georeferencing ...")
        stations = calib.get("stations", [])
        px2ll, rms = fit_affine(calib["control_points"])
        log(f"    affine fit over {len(calib['control_points'])} control points "
            f"(rms {rms:.0f} m)")
        dots_ll = [px2ll(x, y) for x, y in dots_px]
        ns = len(stations)
        coords_arr = np.array([[s["lat"], s["lon"]] for s in stations] +
                               [list(d) for d in dots_ll]).reshape(-1, 2)
        D = haversine_matrix(coords_arr)
        station_names = [s["name"] for s in stations]
        node_to_px = {i: (s["px"], s["py"]) for i, s in enumerate(stations)}
    else:
        log(f"2/{steps} building pixel-space graph (no calibration — "
            "only annotated PDFs will be produced) ...")
        stations = []
        rms = None
        icons = detect_station_icons(img)
        ns = len(icons)
        station_names = [f"{kind.split()[0]} {i + 1}"
                         for i, (kind, _, _) in enumerate(icons)]
        icon_pxs = [(float(x), float(y)) for _, x, y in icons]
        all_px_nodes = icon_pxs + list(dots_px)
        D = euclidean_matrix(all_px_nodes)
        node_to_px = {i: pxpos for i, pxpos in enumerate(icon_pxs)}

    for i, p in enumerate(dots_px):
        node_to_px[ns + i] = p
    dot_idx = list(range(ns, ns + len(dots_px)))

    # --- 3. solve TSP variants ---
    log(f"3/{steps} solving routes ...")
    variants = []

    # variant A: open path between two station nodes (best pair, or forced)
    if ns >= 2 or (start and end and calibrated):
        if start and end and calibrated:
            pairs = [(station_names.index(start), station_names.index(end))]
        else:
            min_gap = 150 if calibrated else 10
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
        suffix = f"{lenA/1000:.2f} km" if calibrated else f"{lenA:.0f} px"
        log(f"    A  {station_names[sA]} -> {station_names[eA]}: {suffix}")

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
        suffix = f"{lenB/1000:.2f} km" if calibrated else f"{lenB:.0f} px"
        log(f"    B  loop from {station_names[depot]}: {suffix}")

    # variant C: shortest free circle over dots only, no fixed start/end
    orderC, lenC = solve_circle(D, dot_idx)
    variants.append(dict(
        key="circle", order=orderC, bird=lenC,
        name="Hofflohmaerkte circular tour (dots only)",
        title="Rundtour ueber alle Hoefe (freier Start)"))
    suffix = f"{lenC/1000:.2f} km" if calibrated else f"{lenC:.0f} px"
    log(f"    C  free circle: {suffix}")

    # --- 4-5. GPS exports (calibrated mode only) ---
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

        log(f"4/{steps} fetching walking geometry (OSRM) ...")
        for v in variants:
            v["track"] = v["dist"] = v["dur"] = None
            if use_osrm:
                track, dist, dur = osrm_geometry(
                    [(la, lo) for la, lo, _ in v["stops"]])
                v["track"], v["dist"], v["dur"] = track, dist, dur
                if dist:
                    log(f"    {v['key']}: {dist/1000:.2f} km on streets "
                        f"(~{dur/3600:.1f} h pure walking)")

        log(f"5/{steps} writing GPS exports ...")
        triples = [(v["name"], v["stops"], v["track"]) for v in variants]
        for v in variants:
            write_gpx(out / f"route_{v['key']}.gpx", v["name"],
                      v["stops"], v["track"])
            write_kml(out / f"route_{v['key']}.kml", v["name"],
                      v["stops"], v["track"])
            v["gmaps"] = gmaps_overview_link(v["stops"])
        write_geojson(out / "routes.geojson", triples)
        write_html(out / "routes_map.html", "Hofflohmaerkte routes",
                   triples, stations)
        txt = ["# Google Maps - one walking link per route (whole route in one",
               "# shot, downsampled to Google's 9-waypoint URL limit; import the",
               "# .kml into Google My Maps for the exact full line).", ""]
        for v in variants:
            txt += [f"## {v['name']}", v["gmaps"], ""]
        txt += ["# Appendix: exact stop-by-stop legs (9 waypoints per link)", ""]
        for v in variants:
            links = gmaps_links(v["stops"])
            txt.append(f"## {v['name']} ({len(links)} legs)")
            txt += [f"{k + 1}. {u}" for k, u in enumerate(links)]
            txt.append("")
        (out / "google_maps_links.txt").write_text("\n".join(txt))

    # --- last step: annotate PDFs + previews (both modes) ---
    log(f"{steps}/{steps} annotating PDFs + previews ...")
    fitz.open(pdf_path)[0].get_pixmap(dpi=110).save(out / "original.png")
    for v in variants:
        order_px = [node_to_px[n] for n in v["order"]]
        if calibrated:
            km = f"{(v.get('dist') or v['bird']) / 1000:.1f} km"
            title_str = f"{v['title']} | {len(dots_px)} Hoefe | ~{km}"
        else:
            title_str = f"{v['title']} | {len(dots_px)} Hoefe"
        pdf_out = out / f"route_{v['key']}.pdf"
        # Label every detected icon. Use the named station from calibration
        # when one is close (within 30 px); otherwise show a short type
        # label ("U" or "S").
        _named = {(s["px"], s["py"]): s["name"] for s in stations} \
                 if calibrated else {}
        station_labels = []
        for kind, x, y in detect_station_icons(img):
            best_name, best_dist = None, float("inf")
            for (sx, sy), sname in _named.items():
                d = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
                if d < best_dist:
                    best_dist, best_name = d, sname
            label = best_name if best_dist < 30 else kind[0]
            station_labels.append((float(x), float(y), label))
        station_labels = station_labels or None
        annotate_pdf(pdf_path, pdf_out, order_px, title_str, dpi=dpi,
                     station_labels=station_labels)
        fitz.open(pdf_out)[0].get_pixmap(dpi=110).save(
            out / f"route_{v['key']}.png")

    return {
        "dots": len(dots_px),
        "fit_rms_m": round(rms, 1) if rms is not None else None,
        "variants": [{
            "key": v["key"], "name": v["name"],
            "bird_km": round(v["bird"] / 1000, 2) if calibrated else None,
            "street_km": round(v["dist"] / 1000, 2)
                         if calibrated and v.get("dist") else None,
            "walk_h": round(v["dur"] / 3600, 1)
                      if calibrated and v.get("dur") else None,
            "pdf": f"route_{v['key']}.pdf",
            "gpx": f"route_{v['key']}.gpx" if calibrated else None,
            "kml": f"route_{v['key']}.kml" if calibrated else None,
            "png": f"route_{v['key']}.png",
            "gmaps": v.get("gmaps") if calibrated else None,
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
    if args.calib:
        calib = json.loads(Path(args.calib).read_text())
    else:
        calib = None
        print("No --calib provided — running in pixel-only mode "
              "(annotated PDFs only, no GPS exports).\n"
              "Run --find-landmarks to create a calibration file.")

    run_pipeline(pdf_path, calib, out, dpi=args.dpi, start=args.start,
                 end=args.end, use_osrm=not args.no_osrm)
    print(f"\nDone -> {out}/")
    for f in sorted(out.iterdir()):
        print("   ", f.name)


if __name__ == "__main__":
    main()
