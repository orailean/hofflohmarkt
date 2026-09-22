# Flyer Street Routing and Station Labels

## Purpose

Hoffroute must produce an annotated Hof- und Gartenflohmärkte flyer that is useful without manual route interpretation. The visible tour must include every detected market marker, return to its starting marker, follow the streets printed on the flyer, and identify transit icons by their real station names. The implementation must not require a paid API or expose a configuration switch that permits straight-line routing.

The Aubing flyer at `hofflohmaerkte-aubing-190926.pdf` is the primary acceptance fixture. The design must also work with other flyers using the same visual language.

## Confirmed Problems

The Aubing run detected 61 market markers, and its exported geographic route contained all 61 markers plus a repeated start. Its OSRM track was also numerically closed. The PDF nevertheless appeared to skip markers because OSRM snapped the geographic waypoints to walkable edges: 27 markers were more than 10 m from the returned track, with a maximum gap of 54 m.

The geographic track was then projected onto the flyer with an automatically fitted affine transform. That transform had a 66 m RMS error and was derived from geocoded street-name centroids, which are not the positions of the labels on the flyer. As a result, real OSM streets did not align with the stylized flyer streets.

Both detected S-Bahn icons were rendered with the generic label `S-Bahn`. The automatic station resolver returned no stations after one Overpass timeout and one unmatched result, and the renderer treated the transit type as a station name.

These are separate failures and must be validated separately: marker inclusion, route closure, PDF street adherence, geographic routing, and station-name resolution.

## Selected Architecture

The annotated PDF and the geographic exports will share an ordered list of market stops but use geometry suited to their respective coordinate systems:

- A new flyer street-network component will extract the printed street network and calculate the PDF overlay directly in pixel coordinates.
- The free FOSSGIS OSRM service will calculate the corresponding geographic route for GPX, KML, GeoJSON, and the interactive OSM map.
- A route validator will verify inclusion and closure before any artifact is published.
- A resilient free-data station resolver will assign real names to detected transit icons. A transit type will never be presented as a station name.

Street-following behavior is unconditional. There will be no user-facing option to replace it with straight lines.

## Flyer Street Network

### Road-mask extraction

The rendered first page will be converted to a road mask using the flyer’s pale-blue street styling. The extractor will operate in a perceptual color space and combine color distance, saturation, and brightness thresholds rather than depend on one exact RGB value. Magenta market dots, green transit icons, dark labels, logos, page borders, and annotation colors must be excluded.

Small gaps caused by text printed over roads will be closed morphologically, subject to maximum gap and direction constraints. Large white regions must not be bridged. The implementation will use the existing NumPy and SciPy image-processing dependencies; no hosted vision service is introduced.

The mask will be skeletonized and converted into a weighted graph. Graph nodes represent junctions, endpoints, and sampled bend points. Edge weights are pixel arc lengths. Short isolated components and page-decoration components will be discarded using size, location, and proximity-to-market criteria.

### Stop access points

Each detected market marker will be assigned to the nearest reachable point on the street graph. At 300 DPI the maximum access distance is 90 pixels; at other render resolutions it scales linearly with DPI. The assignment retains both coordinates:

- the original market-dot center, used for the flag/number and inclusion accounting;
- the street access point, used for graph routing.

The renderer will draw the main route on the street graph and a visually distinct short access spur between each market dot and its street access point. This makes courtyard access explicit without making the main path appear to cut through blocks.

If a marker cannot be connected within the supported radius, generation must fail with the marker number and a diagnostic preview. It must not silently omit the marker or draw an unrestricted straight segment to another stop.

### Ordering and route geometry

The street graph will provide pairwise shortest-path distances between stop access points. The existing circle solver will optimize against this graph-distance matrix, not Euclidean pixel distance and not a poorly calibrated geographic matrix. The selected order will contain every market marker exactly once, followed by the first marker once more.

The final PDF polyline is the concatenation of the graph shortest paths between each successive pair, including the final pair back to the start. Adjacent duplicate points will be removed, but the closure segment must remain. The start flag is drawn at the original start marker; circular tours do not draw an end flag.

## Geographic Route and Calibration

OSRM remains the geographic route provider because it is free, returns walking-network geometry, and produces useful portable exports. It receives the validated stop sequence including the repeated start. Returned waypoint snap locations will be retained so geographic access gaps can be measured and reported.

Automatic affine calibration will no longer be used as evidence that the PDF overlay aligns with streets. Its quality report will include control-point coverage and residual error; a three-point zero residual is not considered proof of accuracy. Poor calibration may prevent accurate geographic exports, but it cannot degrade the independent PDF street route.

The OSRM output must satisfy all of these checks:

- one waypoint result for every supplied stop occurrence;
- first and last route coordinates within 2 m for a circular tour;
- no missing route leg;
- a recorded snap distance for every market marker.

OSRM or calibration failures produce an explicit geographic-export error. They do not cause straight-line PDF routing and do not silently publish misleading geographic files.

## Station Name Resolution

Station resolution will use only free OpenStreetMap data and local caching. It will work in stages:

1. Infer a broad district search area from the flyer context rather than relying on a small radius around one inaccurate projected icon.
2. Query all named transit candidates of the required mode in that area. Overpass endpoints may be retried with a bounded timeout; a Nominatim station search provides a second free-data path.
3. Match all detected icons and all candidates as a group. The score combines transit mode, relative icon layout, approximate calibration, and distance. One candidate cannot be assigned to two icons.
4. Cache the resolved name, OSM identity, mode, and coordinates with a resolver-version key.

For every resolved icon, the PDF callout contains the actual name and mode, for example `Aubing` with `S-BAHN`. `START`, `ZIEL`, or `START / ZIEL` is added only when the chosen route role applies to that station.

If resolution remains ambiguous, the renderer must use an explicit `Station name unavailable` diagnostic and the job result must contain a warning. It must not substitute `S-Bahn`, `U-Bahn`, or another generic mode as the name. The Aubing acceptance fixture requires real names for both detected S-Bahn icons.

## Validation and Publication

A variant is published only after a shared validator confirms:

- detected market count equals the count of unique ordered market stops;
- no market marker appears twice before the closing occurrence;
- the closing occurrence is the same marker as the start;
- every successive stop pair has a flyer-graph path;
- every main-route sample lies on or within 2 pixels of the extracted street mask at 300 DPI, with the tolerance scaled linearly at other resolutions;
- every original market marker has an access spur;
- geographic files, when present, use the same stop order;
- station callouts never use a generic transit mode as their name.

The result JSON will expose marker count, closure status, maximum access-spur length, unresolved-station warnings, geographic snap statistics, and calibration quality. Cached calibration and route artifacts receive new version keys so previously generated misleading output cannot be reused.

## User Experience and Failure Handling

The existing upload-and-generate flow remains unchanged. Street-following is the default and only route mode.

Successful results distinguish the two geometries in plain language:

- `Flyer route`: follows the streets drawn on the uploaded flyer;
- `GPS route`: follows OpenStreetMap walking data and may use different access points.

Failures are actionable. Examples include `Market marker 17 is not connected to the printed street network`, `Could not resolve the name of one S-Bahn station`, and `GPS export unavailable because automatic map calibration is unreliable`. A PDF route that passes its independent validation may still be delivered when only geographic exports fail.

## Testing Strategy

Implementation follows test-driven development.

Unit fixtures will cover road-color variation, label-sized gaps, crossing streets, disconnected decoration, stop-to-road snapping, shortest paths, and circular concatenation. Tests will assert behavior from pixels and route outputs rather than private implementation details.

A synthetic flyer fixture will contain known roads and off-road market dots. Its expected tour will prove that every dot is connected, the main route stays on the road mask, access spurs are present, and the final segment closes the loop.

The Aubing integration fixture will assert:

- exactly 61 market markers, excluding the legend marker;
- 61 unique tour stops plus the repeated start;
- a closed PDF street route;
- no unrestricted block-crossing segment in the main route;
- an access spur for every market marker;
- the two S-Bahn icons named `Aubing` and `Leienfelsstraße`, rather than generic mode labels;
- consistent stop ordering across PDF metadata and geographic exports.

The Aubing test uses a checked-in station-candidate response fixture rather than depending on live network availability. Existing tests for dot detection, OSRM request handling, cache keys, rendering, and the web endpoint remain green. A rendered PNG of the Aubing output will be visually inspected after automated checks to catch label overlap and masking artifacts.

## Scope Boundaries

This change does not introduce paid routing, geocoding, map, OCR, or vision services. It does not attempt turn-by-turn written directions, live transit data, multi-page flyer routing, or arbitrary cartographic style recognition. The street extractor targets the visual conventions used by the supported Hof- und Gartenflohmärkte flyers and reports unsupported layouts explicitly.
