# Flyer Street Routing and Station Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a closed, every-stop tour whose PDF overlay follows the flyer’s printed streets and whose transit callouts contain real station names, using only local processing and free OpenStreetMap services.

**Architecture:** A focused `flyer_streets.py` module extracts a pale-blue street mask, skeletonizes it, exposes stop snapping and shortest-path geometry, and validates PDF routes. `hoffroute.py` uses that graph for visit ordering and annotated-PDF geometry while retaining FOSSGIS OSRM for GPS exports. `webapp.py` resolves transit icons from a district-wide candidate set, matches icons jointly, versions caches, and reports partial geographic-export failures without weakening the flyer route.

**Tech Stack:** Python 3.12, NumPy, SciPy `ndimage`/`sparse.csgraph`, PyMuPDF, FastAPI, unittest, free FOSSGIS OSRM, Overpass, and Nominatim.

**Spec:** `docs/superpowers/specs/2026-09-21-flyer-street-routing-design.md`

## Global Constraints

- No paid routing, geocoding, OCR, map, or vision service.
- Street-following is unconditional; no straight-line or user-facing routing toggle.
- At 300 DPI a market marker may be at most 90 pixels from its street access point; distance and tolerances scale linearly with DPI.
- The PDF main route stays within 2 pixels of the extracted street mask at 300 DPI; access spurs are rendered separately.
- A circular order contains every market marker exactly once and repeats only the first marker at the end.
- Generic values such as `S-Bahn` and `U-Bahn` are transit modes, never station names.
- The Aubing fixture contains 61 market markers and resolves the two S-Bahn icons as `Aubing` and `Leienfelsstraße`.
- Previously generated calibration and route cache entries must not be reused after this change.

## Review Focus

- A pale-blue decorative line near the footer must be discarded rather than joined to the road graph; Task 1 adds a disconnected-decoration test.
- A street label may erase several pixels of a road, but gap closing must not bridge a block; Task 1 tests both a label-sized break and a wider break.
- Multiple market dots can snap to the same street pixel; Task 2 verifies a zero-length graph leg remains valid and both stops remain in the tour.
- OSRM can snap a waypoint tens of metres away or return chunk-boundary waypoints twice; Task 4 verifies waypoint count, de-duplication, closure, and snap statistics.
- Overpass can time out or return duplicate platform/station objects; Task 5 tests fallback, deduplication, one-to-one group matching, and explicit unresolved warnings.

---

### Task 1: Extract a Navigable Flyer Street Graph

**Files:**
- Create: `flyer_streets.py`
- Create: `tests/test_flyer_streets.py`
- Modify: `docs/superpowers/specs/2026-09-21-flyer-street-routing-design.md`

**Interfaces:**
- Consumes: an RGB `numpy.ndarray`, `(x0, y0, x1, y1)` map bounds, and render DPI.
- Produces: `FlyerStreetGraph.from_image(image, bbox, dpi) -> FlyerStreetGraph`; public attributes `road_mask`, `skeleton`, `node_xy`, and `adjacency`; exception `FlyerStreetError`.

- [ ] **Step 1: Write failing mask and graph tests**

Create literal synthetic RGB fixtures with pale-blue orthogonal roads, a six-pixel label gap, a forty-pixel block gap, magenta dots, and a disconnected footer line. Tests must assert that road pixels are selected, colored markers are excluded, the six-pixel gap is connected, the forty-pixel gap remains disconnected, and the footer component is absent.

```python
PALE_BLUE = np.array([190, 218, 247], dtype=np.uint8)
MAGENTA = np.array([210, 20, 100], dtype=np.uint8)

def synthetic_street_map(label_gap=None, block_gap=None):
    image = np.full((300, 400, 3), 255, dtype=np.uint8)
    image[97:104, 50:351] = PALE_BLUE
    image[50:221, 197:204] = PALE_BLUE
    image[277:284, 120:281] = PALE_BLUE
    if label_gap:
        image[97:104, 197:197 + label_gap] = 255
    if block_gap:
        left = 200 - block_gap // 2
        image[97:104, left:left + block_gap] = 255
    image[91:110, 191:210] = MAGENTA
    return image

class FlyerStreetExtractionTests(unittest.TestCase):
    def test_extracts_roads_closes_label_gap_and_discards_decoration(self):
        image = synthetic_street_map(label_gap=6, block_gap=None)
        graph = FlyerStreetGraph.from_image(image, (20, 20, 380, 260), dpi=300)
        self.assertTrue(graph.road_mask[100, 80])
        self.assertFalse(graph.road_mask[100, 200])  # magenta market dot
        self.assertTrue(graph.connected((80, 100), (320, 100)))
        self.assertFalse(graph.road_mask[280, 200])  # footer decoration

    def test_does_not_bridge_a_wide_gap_across_a_block(self):
        image = synthetic_street_map(label_gap=None, block_gap=40)
        graph = FlyerStreetGraph.from_image(image, (20, 20, 380, 260), dpi=300)
        self.assertFalse(graph.connected((80, 100), (320, 100)))
```

- [ ] **Step 2: Run the focused tests and observe the missing-module failure**

Run: `python3 -m unittest -v tests.test_flyer_streets.FlyerStreetExtractionTests`

Expected: FAIL because `flyer_streets` does not exist.

- [ ] **Step 3: Implement color masking, constrained closing, skeletonization, and an 8-neighbour sparse graph**

Use NumPy channel/color-distance predicates plus `scipy.ndimage` morphology. Implement iterative morphological skeletonization locally so no new binary dependency is required. Retain connected components that are inside the map bounds and either exceed the minimum network size or lie near a detected map-region road component. Build a CSR adjacency matrix with weights `1` and `sqrt(2)`.

```python
class FlyerStreetError(ValueError):
    pass

@dataclass
class FlyerStreetGraph:
    road_mask: np.ndarray
    skeleton: np.ndarray
    node_xy: np.ndarray
    adjacency: scipy.sparse.csr_matrix
    pixel_to_node: np.ndarray
    dpi: int

    @classmethod
    def from_image(cls, image, bbox, dpi=300):
        road_mask = extract_road_mask(image, bbox, dpi)
        skeleton = morphological_skeleton(road_mask)
        return cls.from_skeleton(road_mask, skeleton, dpi)

    def connected(self, first_xy, second_xy):
        first = self.nearest_node(first_xy, max_distance=4)
        second = self.nearest_node(second_xy, max_distance=4)
        return np.isfinite(csgraph.dijkstra(
            self.adjacency, indices=first, limit=np.inf)[second])
```

- [ ] **Step 4: Run extraction tests**

Run: `python3 -m unittest -v tests.test_flyer_streets.FlyerStreetExtractionTests`

Expected: PASS.

- [ ] **Step 5: Commit the component**

```bash
git add flyer_streets.py tests/test_flyer_streets.py docs/superpowers/specs/2026-09-21-flyer-street-routing-design.md
git commit -m "Add flyer street network extraction"
```

### Task 2: Snap Every Stop and Build Closed Street Geometry

**Files:**
- Modify: `flyer_streets.py`
- Modify: `tests/test_flyer_streets.py`

**Interfaces:**
- Consumes: `FlyerStreetGraph`, original `(x, y)` stop centers, an ordered list of access indices.
- Produces: immutable `StreetAccess(stop_xy, street_xy, node, distance_px)`; `snap_stops(stops, max_distance_px) -> list[StreetAccess]`; `distance_matrix(accesses) -> ndarray`; `route_geometry(order, accesses) -> list[(x, y)]`; `validate_closed_route(order, accesses, route) -> RouteValidation`.

- [ ] **Step 1: Write failing stop, distance, closure, and unreachable-stop tests**

```python
def graph_fixture(shape=(200, 200), cross=True):
    mask = np.zeros(shape, dtype=bool)
    mask[50, 20:181] = True
    if cross:
        mask[20:101, 100] = True
    return FlyerStreetGraph.from_skeleton(mask.copy(), mask, dpi=300)

def test_snap_stops_routes_every_stop_and_closes_on_the_street_mask(self):
    graph = graph_fixture()
    stops = [(25, 44), (75, 56), (100, 22)]
    access = graph.snap_stops(stops, max_distance_px=15)
    distances = graph.distance_matrix(access)
    order = [0, 2, 1, 0]
    route = graph.route_geometry(order, access)
    report = graph.validate_closed_route(order, access, route)
    self.assertEqual([a.stop_xy for a in access], stops)
    self.assertEqual(route[0], route[-1])
    self.assertTrue(report.closed)
    self.assertEqual(report.unique_stop_count, 3)
    self.assertLessEqual(report.max_mask_distance_px, 2)
    self.assertTrue(np.isfinite(distances).all())

def test_two_stops_may_share_one_access_node_without_being_dropped(self):
    graph = graph_fixture(cross=False)
    access = graph.snap_stops([(40, 45), (40, 55)], max_distance_px=15)
    self.assertEqual(access[0].node, access[1].node)
    self.assertEqual(graph.distance_matrix(access)[0, 1], 0)

def test_reports_the_number_of_an_unreachable_market_marker(self):
    with self.assertRaisesRegex(FlyerStreetError, "marker 2"):
        graph_fixture(cross=False).snap_stops(
            [(40, 45), (180, 180)], max_distance_px=15)
```

- [ ] **Step 2: Run the focused tests and verify missing-method failures**

Run: `python3 -m unittest -v tests.test_flyer_streets.FlyerStreetRoutingTests`

Expected: FAIL because `snap_stops`, `distance_matrix`, and `route_geometry` are absent.

- [ ] **Step 3: Implement nearest-node snapping and sparse shortest paths**

Use a `scipy.spatial.cKDTree` over `node_xy`. Run `scipy.sparse.csgraph.dijkstra(self.adjacency, indices=unique_access_nodes, return_predecessors=True)` once for the unique access nodes, reconstruct each successive route leg, concatenate without duplicate junction pixels, and preserve the final return leg. Raise `FlyerStreetError` with the one-based market number for out-of-range or disconnected access points.

```python
@dataclass(frozen=True)
class StreetAccess:
    stop_xy: tuple[float, float]
    street_xy: tuple[float, float]
    node: int
    distance_px: float

@dataclass(frozen=True)
class RouteValidation:
    closed: bool
    unique_stop_count: int
    max_access_distance_px: float
    max_mask_distance_px: float
```

- [ ] **Step 4: Run all flyer-street tests**

Run: `python3 -m unittest -v tests.test_flyer_streets`

Expected: PASS.

- [ ] **Step 5: Commit routing behavior**

```bash
git add flyer_streets.py tests/test_flyer_streets.py
git commit -m "Route every flyer stop on printed streets"
```

### Task 3: Integrate Flyer Geometry into Route Generation and PDF Rendering

**Files:**
- Modify: `hoffroute.py:621-735,760-1000`
- Modify: `tests/test_hoffroute.py`

**Interfaces:**
- Consumes: Task 2 `FlyerStreetGraph`, `StreetAccess`, `RouteValidation`; existing TSP solvers and optional geographic calibration.
- Produces: `annotate_pdf(src_doc_path, out_path, order_px, title, color, dpi, station_labels, route_px, access_spurs)`; summary fields `closed`, `unique_stops`, `max_access_spur_px`, and `max_road_offset_px`; a useful PDF route even when geographic export fails.

- [ ] **Step 1: Replace the old synthetic PDF with a real street fixture and write failing pipeline tests**

Draw pale-blue streets and four off-street magenta dots into the PDF fixture. Assert that the fake OSRM detour is absent from the annotated PDF, every access spur is drawn, the street path is closed, and the summary reports four unique stops.

```python
def test_pipeline_draws_closed_flyer_graph_route_with_every_access_spur(self):
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        pdf = temp_path / "map.pdf"
        output = temp_path / "out"
        self._write_street_map_pdf(pdf)
        summary = hr.run_pipeline(
            pdf, self._calibration(), output, dpi=72,
            street_router=FakeStreetRouter(), log=lambda _: None)
        circle = next(v for v in summary["variants"]
                      if v["key"] == "circle")
        self.assertTrue(circle["closed"])
        self.assertEqual(circle["unique_stops"], 4)
        self.assertEqual(circle["access_spurs"], 4)
        self.assertLessEqual(circle["max_road_offset_px"], 2 * 72 / 300)
        with fitz.open(output / "route_circle.pdf") as document:
            blue_routes = [d for d in document[0].get_drawings()
                           if d["color"] is not None and
                           np.allclose(d["color"], (0.11, 0.46, 0.84))]
        self.assertTrue(blue_routes)
```

Add a separate test proving `run_pipeline(pdf, None, output, dpi=72, log=lambda _: None)` still emits the annotated flyer PDF and reports geographic exports unavailable instead of rejecting the job.

- [ ] **Step 2: Run the pipeline tests and verify they fail on OSRM-projected PDF geometry and mandatory calibration**

Run: `python3 -m unittest -v tests.test_hoffroute.PipelineTests`

Expected: FAIL because the returned OSRM geometry is still projected onto the flyer, no access spurs exist, and missing calibration raises before flyer routing.

- [ ] **Step 3: Make the flyer graph the single source of PDF order and geometry**

In `run_pipeline`, construct `FlyerStreetGraph` immediately after dot/icon detection. Snap market dots and resolved station icons, build the graph distance matrix, solve variants from it, and use `route_geometry` for `route_px`. Geographic coordinates and OSRM must reuse the resulting order rather than influence it. Keep station variants only for resolved stations that successfully snap to the graph.

Change `annotate_pdf` to render the main route solid blue and each `(stop_xy, street_xy)` access spur as a thinner dashed or lower-opacity segment before flags and numbers.

```python
def annotate_pdf(src_doc_path, out_path, order_px, title,
                 color=(0.83, 0.07, 0.41), dpi=300,
                 station_labels=None, route_px=None, access_spurs=None):
    route_pts = [fitz.Point(x * 72 / dpi, y * 72 / dpi)
                 for x, y in (route_px or order_px)]
    spur_pts = [
        (fitz.Point(ax * 72 / dpi, ay * 72 / dpi),
         fitz.Point(bx * 72 / dpi, by * 72 / dpi))
        for (ax, ay), (bx, by) in (access_spurs or [])
    ]
    spur_shape = page.new_shape()
    for start_point, street_point in spur_pts:
        spur_shape.draw_line(start_point, street_point)
    spur_shape.finish(color=(0.11, 0.46, 0.84), width=1.1,
                      dashes="[2 2]", stroke_opacity=0.65)
    spur_shape.commit()
    route_shape = page.new_shape()
    route_shape.draw_polyline(route_pts)
    route_shape.finish(color=(0.11, 0.46, 0.84), width=2.2,
                       lineJoin=1, lineCap=1, stroke_opacity=0.8)
    route_shape.commit()
```

Geographic export becomes an optional second phase: catch only `StreetRoutingError` and unreliable-calibration errors, record `gps_available=False` plus a warning, and continue producing validated PDF files. Programming errors continue to fail the job.

- [ ] **Step 4: Run route and rendering tests**

Run: `python3 -m unittest -v tests.test_hoffroute.PipelineTests`

Expected: PASS.

- [ ] **Step 5: Commit integration**

```bash
git add hoffroute.py tests/test_hoffroute.py
git commit -m "Use printed streets for PDF route geometry"
```

### Task 4: Retain and Validate OSRM Snapped Waypoints

**Files:**
- Modify: `hoffroute.py:385-505,760-1000`
- Modify: `tests/test_hoffroute.py`

**Interfaces:**
- Consumes: ordered geographic `(lat, lon)` stops including the repeated start.
- Produces: `StreetRoute(geometry, distance_m, duration_s, snapped_waypoints, snap_distances_m)`; `validate_geographic_route(route, expected_waypoints, circular)`.

- [ ] **Step 1: Write failing route-result tests**

```python
def test_route_retains_one_snapped_waypoint_per_input_across_chunks(self):
    coords = [(48.0, 11.0), (48.1, 11.1),
              (48.2, 11.2), (48.3, 11.3)]
    result = router.route(coords)
    self.assertEqual(len(result.snapped_waypoints), 4)
    self.assertEqual(result.snapped_waypoints[0], (48.0, 11.0))
    self.assertEqual(result.snapped_waypoints[-1], (48.3, 11.3))

def test_circular_route_rejects_open_osrm_geometry(self):
    result = StreetRoute(
        geometry=[(48.0, 11.0), (48.001, 11.001)],
        distance_m=100, duration_s=80,
        snapped_waypoints=[(48.0, 11.0)] * 3,
        snap_distances_m=[0, 0, 0])
    with self.assertRaisesRegex(StreetRoutingError, "not closed"):
        validate_geographic_route(result, expected_waypoints=3, circular=True)
```

Also assert that a 54 m snap is preserved in summary statistics rather than treated as a missing stop.

- [ ] **Step 2: Run StreetRouter tests and verify tuple/result mismatch failures**

Run: `python3 -m unittest -v tests.test_hoffroute.StreetRouterTests`

Expected: FAIL because `StreetRouter.route` returns a three-tuple and discards `waypoints`.

- [ ] **Step 3: Implement the structured result, chunk-boundary waypoint joining, and validation**

```python
@dataclass(frozen=True)
class StreetRoute:
    geometry: list[tuple[float, float]]
    distance_m: float
    duration_s: float
    snapped_waypoints: list[tuple[float, float]]
    snap_distances_m: list[float]
```

For later chunks, drop the first returned waypoint because it duplicates the preceding chunk’s final input. Calculate snap distance from each original input with the existing haversine helper. Validate exact waypoint count, non-empty legs, and at most 2 m between first/last geometry points for circular routes. Update fakes and consumers to use named fields.

- [ ] **Step 4: Run StreetRouter and pipeline tests**

Run: `python3 -m unittest -v tests.test_hoffroute.StreetRouterTests tests.test_hoffroute.PipelineTests`

Expected: PASS.

- [ ] **Step 5: Commit geographic validation**

```bash
git add hoffroute.py tests/test_hoffroute.py
git commit -m "Validate OSRM closure and waypoint snapping"
```

### Task 5: Resolve Station Names as a Group with Free-Data Fallback

**Files:**
- Create: `station_resolver.py`
- Create: `tests/fixtures/aubing_transit_candidates.json`
- Create: `tests/test_station_resolver.py`
- Modify: `webapp.py:700-850`
- Modify: `tests/test_webapp.py`

**Interfaces:**
- Consumes: detected icons `(kind, px, py)`, control points, context string, provider callables returning full OSM elements.
- Produces: `resolve_station_icons(icons, control_points, context, candidate_providers) -> StationResolution`; `StationResolution.stations`; `StationResolution.warnings`; each station has `name`, `mode`, `px`, `py`, `lat`, `lon`, and `osm_id`.

- [ ] **Step 1: Check in a deterministic Aubing candidate fixture and write failing matching tests**

The fixture contains `icons`, `control_points`, and complete OSM-style elements for `Aubing`, `Leienfelsstraße`, duplicate platform objects, and nearby irrelevant bus/rail candidates. The icon pixels are `(253.1, 1640.2)` and `(1427.2, 1750.2)` and the five control points are copied from the diagnosed Aubing job.

```python
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/aubing_transit_candidates.json")
    .read_text(encoding="utf-8"))

def test_matches_aubing_icons_jointly_to_real_unique_station_names(self):
    result = resolve_station_icons(
        FIXTURE["icons"], FIXTURE["control_points"], "Aubing, München",
        candidate_providers=[lambda *_: FIXTURE["elements"]])
    self.assertEqual([s["name"] for s in result.stations],
                     ["Aubing", "Leienfelsstraße"])
    self.assertEqual(len({s["osm_id"] for s in result.stations}), 2)
    self.assertEqual(result.warnings, [])

def test_uses_second_provider_after_overpass_timeout(self):
    def raising_timeout(*_args):
        raise TimeoutError("Overpass timed out")
    result = resolve_station_icons(
        FIXTURE["icons"], FIXTURE["control_points"], "Aubing, München",
        candidate_providers=[raising_timeout, lambda *_: FIXTURE["elements"]])
    self.assertEqual(len(result.stations), 2)

def test_unresolved_icon_gets_warning_not_generic_station_name(self):
    result = resolve_station_icons(
        FIXTURE["icons"][:1], FIXTURE["control_points"], "Aubing, München",
        candidate_providers=[lambda *_: []])
    self.assertEqual(result.stations, [])
    self.assertIn("Station name unavailable", result.warnings[0])
```

- [ ] **Step 2: Run resolver tests and verify the missing-module failure**

Run: `python3 -m unittest -v tests.test_station_resolver`

Expected: FAIL because `station_resolver` does not exist.

- [ ] **Step 3: Implement candidate normalization, deduplication, and joint assignment**

Deduplicate OSM station/platform objects by normalized name plus proximity. Filter candidates with the existing mode rules. Score complete icon-to-candidate permutations with mode compatibility, pairwise vector direction, relative distance ratios, approximate affine distance after removing shared translation, and district-name relevance. Reject ambiguous best/second-best scores inside the documented margin rather than inventing a label.

```python
@dataclass(frozen=True)
class StationResolution:
    stations: list[dict]
    warnings: list[str]

def resolve_station_icons(icons, control_points, context,
                          candidate_providers):
    candidates = fetch_until_nonempty(candidate_providers, context,
                                      control_points)
    return match_station_group(icons, control_points, context, candidates)
```

- [ ] **Step 4: Add district-wide Overpass and Nominatim providers and integrate webapp**

Expand the control-point geographic bounds by 3 km, query named rail/public-transport objects once for the full bounding box, and cap results. The Nominatim fallback searches `S-Bahn <context>` and `U-Bahn <context>` with `format=jsonv2`, `limit=50`, and address details. Preserve the shared global request budget and log provider failures without aborting later providers. Pass the detected context from `auto_calibrate`; persist warnings in calibration and result JSON.

- [ ] **Step 5: Run resolver and webapp tests**

Run: `python3 -m unittest -v tests.test_station_resolver tests.test_webapp`

Expected: PASS.

- [ ] **Step 6: Commit station resolution**

```bash
git add station_resolver.py tests/fixtures/aubing_transit_candidates.json tests/test_station_resolver.py webapp.py tests/test_webapp.py
git commit -m "Resolve flyer transit icons to station names"
```

### Task 6: Remove Generic Labels and Expose Validation Results

**Files:**
- Modify: `hoffroute.py:621-735,760-1020`
- Modify: `webapp.py:40-70,1000-1170`
- Modify: `static/index.html`
- Modify: `static/i18n/de.json`
- Modify: `static/i18n/en.json`
- Modify: `static/i18n/ro.json`
- Modify: `tests/test_hoffroute.py`
- Modify: `tests/test_webapp.py`

**Interfaces:**
- Consumes: route/station validation data from Tasks 2–5.
- Produces: user-visible `Flyer route` and `GPS route` status; warnings; cache versions `flyer-streets-v1` and `station-resolver-v1`; no generic station-name fallback.

- [ ] **Step 1: Write failing renderer, response, and cache-version tests**

```python
def test_generic_transit_mode_is_not_rendered_as_station_name(self):
    labels, warnings = hr.station_labels_for_icons(
        [("S-Bahn (green icon)", 100, 100)], stations=[])
    self.assertEqual(labels, [])
    self.assertIn("Station name unavailable", warnings[0])

def test_result_distinguishes_flyer_and_gps_route_status(self):
    summary = {
        "flyer_route": {"available": True},
        "gps_route": {
            "available": False,
            "warning": "GPS export unavailable: calibration is unreliable",
        },
        "variants": [], "files": [],
    }
    response = webapp.build_response(summary, [])
    self.assertTrue(response["flyer_route"]["available"])
    self.assertFalse(response["gps_route"]["available"])
    self.assertIn("calibration", response["gps_route"]["warning"].lower())
```

Add a cache test proving the pre-change key differs after the version bump even with identical PDF/calibration/endpoints.

- [ ] **Step 2: Run focused tests and verify generic-label/status failures**

Run: `python3 -m unittest -v tests.test_hoffroute.PipelineTests tests.test_webapp`

Expected: FAIL because `S-Bahn` is still used as a fallback name and route statuses are not separated.

- [ ] **Step 3: Remove the fallback and propagate diagnostics**

Extract `station_labels_for_icons` so unresolved icons generate warnings and no misleading callout. Add the validation metrics and flyer/GPS availability objects to summaries. Bump route/calibration keys and include the station-resolver version. Update all three translations and the result UI to show flyer and GPS statuses independently.

- [ ] **Step 4: Run all automated tests and static checks**

Run: `python3 -m unittest -v`

Expected: all tests PASS.

Run: `python3 -m py_compile hoffroute.py flyer_streets.py station_resolver.py webapp.py`

Expected: exit 0.

Run: `python3 -m json.tool static/i18n/de.json >/dev/null && python3 -m json.tool static/i18n/en.json >/dev/null && python3 -m json.tool static/i18n/ro.json >/dev/null`

Expected: exit 0.

- [ ] **Step 5: Commit result UX and cache migration**

```bash
git add hoffroute.py webapp.py static/index.html static/i18n/de.json static/i18n/en.json static/i18n/ro.json tests/test_hoffroute.py tests/test_webapp.py
git commit -m "Report flyer and GPS route validation"
```

### Task 7: Aubing End-to-End Acceptance and Documentation

**Files:**
- Modify: `README.md`
- Modify: `tests/test_hoffroute.py` only if the acceptance run exposes a reproducible regression not covered above

**Interfaces:**
- Consumes: the public Aubing PDF and complete application pipeline.
- Produces: inspected annotated PDF/PNG and a final evidence report; no permanent generated job/cache artifacts.

- [ ] **Step 1: Run the full suite before external acceptance**

Run: `python3 -m unittest -v`

Expected: all tests PASS.

- [ ] **Step 2: Download the Aubing acceptance PDF to a temporary directory**

```bash
acceptance_dir=$(mktemp -d)
curl -L 'https://cdn.shopify.com/s/files/1/0683/5339/2946/files/hofflohmaerkte-aubing-190926.pdf?v=1788773101' -o "$acceptance_dir/aubing.pdf"
```

Expected: a PDF beginning with `%PDF`.

- [ ] **Step 3: Execute the real prepare/solve pipeline with fresh versioned caches**

Use the same functions as the FastAPI job path, not a test-only shortcut. Capture `solve_result.json` and assert with a read-only Python command that `dots == 61`, the circle has `unique_stops == 61`, `closed is True`, `access_spurs == 61`, `max_road_offset_px <= 2`, and station names are exactly `Aubing` and `Leienfelsstraße`.

- [ ] **Step 4: Render and visually inspect the generated route PDF**

Run Poppler/PyMuPDF rendering at 150 DPI and inspect the PNG. Confirm the solid route lies on printed pale-blue streets, short spurs reach every magenta marker, the return segment is visible, the legend dot is absent, and station callouts do not obscure the route or use generic names.

- [ ] **Step 5: Update README with the two-geometry behavior and failure semantics**

Document that PDF routes follow printed flyer streets, GPS routes follow OpenStreetMap streets, both use the same stop order, all services are free, and a GPS/calibration failure does not silently produce straight lines.

- [ ] **Step 6: Run final verification**

Run: `python3 -m unittest -v && python3 -m py_compile hoffroute.py flyer_streets.py station_resolver.py webapp.py && git diff --check`

Expected: all tests PASS, compilation exits 0, and `git diff --check` emits no output.

- [ ] **Step 7: Remove only temporary acceptance artifacts and commit documentation**

Delete the explicit `acceptance_dir` created in Step 2 after confirming it is a temporary path. Do not delete repository jobs or caches.

```bash
git add README.md
git commit -m "Document flyer and GPS route behavior"
```
