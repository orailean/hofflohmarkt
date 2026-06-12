# hoffroute — Hofflohmärkte route planner

Takes a Hofflohmärkte map PDF (the official flyer with one red dot per
registered courtyard), finds the shortest walking route that visits **every
dot**, and exports it for navigation apps — plus a copy of the original PDF
with the route drawn on top.

Up to three route variants are produced:

1. **Station to station** — starts at one S-/U-Bahn stop and ends at another
   (the best pair is chosen automatically, or force it with `--start`/`--end`).
2. **Loop** — same start station, returns to it at the end.
3. **Free circle** — the shortest closed tour connecting all dots, with no
   fixed start or end point; begin anywhere on the circle. Always produced —
   and the only variant if the calibration file lists no stations.

The script is map-agnostic: feed it a different district's flyer each time,
the only per-map input is the calibration file (see below).

## How it works

1. Renders the PDF page and detects the red/pink market dots by color
   (touching dots are split via distance-transform peaks).
2. Georeferences pixel positions to GPS coordinates with an affine fit over
   control points from a calibration file (the U/S-Bahn station icons on the
   map, matched to their real-world coordinates).
3. Solves the traveling-salesman problem (nearest neighbor + 2-opt + Or-opt).
4. Fetches the real street-following walking geometry and distance from the
   public [FOSSGIS OSRM](https://routing.openstreetmap.de) foot router
   (skipped with `--no-osrm`).
5. Writes all exports (see below).

## Setup

Requires Python 3.10+.

```bash
./setup.sh                # creates .venv/ and installs requirements.txt
source .venv/bin/activate
```

or manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Web UI

```bash
source .venv/bin/activate
uvicorn webapp:app --port 8000
# or: docker compose up
```

Open http://localhost:8000 and follow the steps: load the flyer (file upload
**or paste a URL**), calibrate on the rendered map (detected station icons are
pre-filled; pick positions by clicking the map, find coordinates with the
built-in place search, drag-select the map area), then compute. Results show
the **original flyer and the route-annotated version side by side**, one
Google Maps link per route, an embedded interactive OpenStreetMap view, and
a download grid with every artifact. Calibrations can be exported/imported
as JSON for reuse.

The UI is available in **English, German, and Romanian** — the browser's
language setting picks the default, and a switcher in the header overrides
it (persisted in the browser). Translations live in separate files under
[static/i18n/](static/i18n/) (`en.json`, `de.json`, `ro.json`); to add a
language, copy `en.json`, translate the values, and add the language code
to the `LANGS` list and the selector in `static/index.html`.

### CLI

```bash
source .venv/bin/activate
python hoffroute.py hofflohmaerkte-haidhausen.pdf \
    --calib calib_haidhausen.json -o route_out

# the PDF can also be a URL:
python hoffroute.py https://example.org/flyer.pdf \
    --calib calib.json -o route_out
```

Options:

| Flag | Meaning |
|------|---------|
| `--calib FILE` | calibration JSON (required, see below) |
| `-o DIR` | output directory (default `route_out`) |
| `--start NAME` / `--end NAME` | force start/end station (names from the calibration file) |
| `--no-osrm` | offline mode — skip the walking-geometry lookup, use straight lines |
| `--dpi N` | render resolution (default 300; calibration pixel coords must match) |
| `--find-landmarks` | calibration helper: detect the U/S station icons, write `calib_template.json` + `map_render.png`, exit (no `--calib` needed) |

## Outputs

| File | What it is |
|------|------------|
| `route_station_to_station.pdf` | original flyer with the route drawn on it — blue line, numbered stops, green **S** = start, red **Z** = end |
| `route_loop.pdf` | same, for the station loop variant |
| `route_circle.pdf` | same, for the free circular tour (single green marker — start anywhere) |
| `route_*.gpx` | waypoints in visiting order + street-following track; import into Komoot, OsmAnd, Organic Maps, Garmin |
| `route_*.kml` | the whole route as **one continuous line** + stops; import into Google My Maps (shows inside the Google Maps app), Google Earth, Organic Maps |
| `route_*.png`, `original.png` | rendered previews (used for the UI's side-by-side view) |
| `routes_map.html` | interactive OpenStreetMap (Leaflet) with all routes toggleable — open in a browser |
| `google_maps_links.txt` | **one Google Maps walking link per route** (whole route in one shot, downsampled to Google's hard 9-waypoint URL limit), plus the exact stop-by-stop legs as an appendix |
| `routes.geojson` | all routes + stops for GIS tools |

All exports are walking-mode: the Google Maps links use `travelmode=walking`
and the KML/GPX/HTML geometry comes from the OSRM **foot** router.

> Note on "one Google Maps link": Google's URL API hard-caps a directions
> link at 9 waypoints, so a 160-stop route cannot be encoded exactly in a
> single link. The overview link follows the route end to end via 9 evenly
> spaced stops; for the exact full line inside Google Maps, import the
> `.kml` into [Google My Maps](https://mymaps.google.com).

## Creating a calibration file for a new map

The only per-map input is a calibration JSON: at least 3 well-spread
landmarks whose pixel position (at `--dpi`, default 300) and GPS coordinates
you know. The U-/S-Bahn station icons on the flyer are ideal because the
script can find them for you.

**Step 1 — run the helper.** It detects the blue U and green S icons and
writes a pre-filled template plus a rendered PNG of the page:

```bash
python hoffroute.py new-map.pdf --find-landmarks -o calib_work
# -> calib_work/calib_template.json  (icon pixel positions already filled in)
# -> calib_work/map_render.png       (the page at 300 dpi, for orientation)
```

**Step 2 — identify each icon.** Open `map_render.png`, find each detected
icon (the template names contain their pixel positions), and see which
station it is from the street/square labels next to it.

**Step 3 — fill in names and GPS coordinates.** On
[openstreetmap.org](https://www.openstreetmap.org) search for the station,
right-click its position → *Show address* — the panel shows `lat, lon`.
Paste them into the template:

```json
{
  "map_bbox_px": [400, 1050, 2430, 2780],
  "control_points": [
    {"name": "Max-Weber-Platz (U)", "px": 947.9, "py": 979.9,
     "lat": 48.1356629, "lon": 11.5978963}
  ],
  "stations": [
    {"name": "Max-Weber-Platz (U)", "px": 947.9, "py": 979.9,
     "lat": 48.1356629, "lon": 11.5978963}
  ]
}
```

**Step 4 — set `map_bbox_px`.** Limit dot detection to the actual map area
`[x0, y0, x1, y1]` so legend/footer artwork (the example dot in the legend,
logos) isn't picked up. Read the pixel values off `map_render.png` in any
image viewer — generous margins are fine as long as decorative red elements
stay outside.

**Step 5 — copy the finished entries into `stations`.** `control_points`
drive the georeferencing fit; `stations` are the start/end candidates for
the station-bound route variants — usually the same entries. Two icons for
the same station (e.g. Ostbahnhof U + S) are fine: keep both as control
points, but list the station only once under `stations`. `stations` is
optional — leave it out (or empty) and the script produces only the free
circular tour.

Notes:

- If the flyer uses different icon colors and the helper finds nothing, you
  can use any landmarks (squares, big crossings): get their pixel position
  from the PNG manually and proceed the same way.
- All pixel coordinates are tied to the render DPI. Stick with the default
  300, or pass the same `--dpi` to both the helper and the main run.
- 3 control points spread over the map corners are enough; more points make
  the fit more robust on heavily stylized maps (the fit is least-squares).
- Sanity check: the main run prints the fit residual (`rms`). With exactly 3
  points it is always 0 — add a 4th known landmark if you want a real check.

## Accuracy caveat

The flyer maps are stylized, so each waypoint can be off by ~30–60 m.
That's fine for walking the route street by street, but don't expect
pin-exact house numbers — the PDFs contain no address list to cross-check.

## Deployment

### Local with Docker Compose

```bash
docker compose up --build
# UI on http://localhost:8000  (override with PORT in .env, see .env.example)
```

Job data (uploaded PDFs, results) lives in the named volume `jobs`; remove it
with `docker compose down -v`.

### Image

Multi-stage build on `python:3.12-slim`, runs as a non-root user, exposes
port 8000, has a `/health` endpoint wired into Docker health checks. The app
needs outbound HTTPS to nominatim.openstreetmap.org (geocoding) and
routing.openstreetmap.de (walking geometry) — both optional, the pipeline
falls back to straight lines without them.

### Pushing to Docker Hub

**One-off push** (replace `you` with your Docker Hub username):

```bash
# 1. build
docker build --target runner -t you/hoffroute:latest .

# 2. log in (once per machine)
docker login

# 3. push
docker push you/hoffroute:latest

# tag a versioned release at the same time
docker tag you/hoffroute:latest you/hoffroute:1.0.0
docker push you/hoffroute:1.0.0
```

**Multi-arch build** (produces a single image that runs on both Intel/AMD and
Apple Silicon / ARM servers — recommended for shared deployments):

```bash
# create a builder that supports multi-platform output (once per machine)
docker buildx create --name multi --use

# build + push in one step
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --target runner \
  -t you/hoffroute:latest \
  --push .
```

The image is ~200 MB on `python:3.12-slim`.  No secrets or API keys are
required at runtime.

**Using a pushed image with docker compose** — set `IMAGE_TAG` in `.env` and
point the compose file at your registry image instead of building locally:

```yaml
# docker-compose.yml  (snippet)
services:
  hoffroute:
    image: you/hoffroute:${IMAGE_TAG:-latest}
    # remove or comment out the `build:` block
```

Then `docker compose pull && docker compose up -d`.

### CI

[.github/workflows/build.yml](.github/workflows/build.yml) builds the image
and smoke-tests `/health` on every push/PR. It contains a commented-out push
block — uncomment it, set `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as
repository secrets in GitHub, and every merge to `main` will publish a fresh
image automatically:

```yaml
# .github/workflows/build.yml  (uncomment to enable)
- name: Log in to Docker Hub
  uses: docker/login-action@v3
  with:
    username: ${{ secrets.DOCKERHUB_USERNAME }}
    password: ${{ secrets.DOCKERHUB_TOKEN }}

- name: Push image
  run: docker push ${{ secrets.DOCKERHUB_USERNAME }}/hoffroute:latest
```

### Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `8000` | host port published by docker compose |
| `APP_PORT` | `8000` | in-container port uvicorn listens on (change when the platform mandates a specific port, e.g. 8080) |
| `IMAGE_TAG` | `latest` | image tag used by docker compose |
| `HOFFROUTE_JOBS_DIR` | `jobs` (app) / `/data/jobs` (container) | where job files are stored |

No secrets are required.

## Files

- [hoffroute.py](hoffroute.py) — the whole pipeline, single file (CLI + library)
- [webapp.py](webapp.py), [static/index.html](static/index.html) — web UI
- [calib_haidhausen.json](calib_haidhausen.json) — calibration for the
  Haidhausen flyer (300 dpi)
- [requirements.txt](requirements.txt), [setup.sh](setup.sh) — environment
- [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml),
  [.env.example](.env.example) — deployment
