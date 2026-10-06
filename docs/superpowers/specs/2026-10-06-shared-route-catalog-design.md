# Shared Route Catalog and First-Visitor Calculation

## Purpose

A map's first visitor may calculate its routes. Later visitors to the same PDF must see and open the saved result immediately, without running route calculation again. A logged-in admin may deliberately recalculate routes through the admin area. Downloads must use filenames tied to the map and route, not generic cache artifact names.

The PDF's SHA-256 content hash identifies a map. A changed PDF is a new map, even if its filename is unchanged. One map may have several existing cached route sets, and the overview shows each set.

## Public experience

The landing page shows a route overview grouped by map. Each entry displays a map title, available route variants, and a way to open the saved result. Opening a cached result loads its summary, previews, interactive map, and artifact links directly from the shared cache; it does not create a job or run the pipeline.

After a visitor uploads a PDF or supplies a URL, the server hashes the PDF before rendering it. If any completed route set exists for that hash, the response contains the matching cached entries immediately. The UI shows those entries and does not offer public calculation for that map. A URL still has to be fetched to determine its content hash. If no route set exists, the current prepare and solve flow remains available to that first visitor.

While the first calculation is in progress, other requests for the same map wait for that result and then use it. If the calculation fails, no incomplete entry is shown, and a later visitor can retry. Different maps can be calculated independently.

## Admin experience and authorization

The existing `HOFFROUTE_MANUAL_USERS` login defines admins. An admin area offers explicit recalculation for a loaded map, including changes to calibration or chosen start and end stations. An admin can also calculate a new map. Public callers cannot use a force or recalculation flag, even by calling the API directly. A public solve request for a map with cached results returns those cached results instead of invoking the pipeline, regardless of the submitted route options.

The admin can upload the source PDF to recalculate an older entry. This is necessary because existing cache directories do not consistently retain their source PDFs. Recalculation publishes its complete output only after success; a failure leaves the previous cached result available.

## Cache catalog and files

Keep the existing disk cache and route artifacts. New entries include metadata with the full PDF hash, original source filename, selected route options, creation time, and enough information to name and list the route set. The catalog reads only complete entries containing a valid summary and required artifacts. Existing cache entries remain visible: their directory's PDF-hash prefix groups them, and any matching job metadata supplies the source name. When no job metadata exists, the route summary supplies a readable title and the hash prefix distinguishes the map.

The cache is shared across users and survives job cleanup and container replacement; Docker Compose must persist the route-cache directory. A per-map lock prevents duplicate first calculations. Finished output is staged before publication, and readers never use a partially written entry. Recalculation preserves the previous published entry until its replacement is ready. Catalog reads do not trigger preparation, geocoding, or route solving.

Artifact URLs are constrained to files listed in the cached summary. Download responses set a map-specific filename that includes the route variant and format, using the existing `artifact_download_names` rules where possible. Previews and the interactive HTML map remain viewable inline.

## API and UI integration

Add public catalog list and cached-result endpoints. The cached-result response has the same result shape as the existing solve-status response, so the current results renderer can display it. The overview and cached-map match view call these endpoints. The existing prepare response gains a cached outcome when a PDF match is found. The solve endpoint enforces first-visitor-only calculation and admin-only recalculation on the server.

Add translated labels and messages in English, German, and Romanian for the overview, cached state, and admin recalculation controls. Document the new flow and cache storage in the README.

## Validation

Tests cover map-hash matching, legacy cache discovery, direct cached result access without a pipeline call, first-visitor calculation, concurrent same-map requests, authorization for forced recalculation, failed rebuild retaining the old result, and map-specific download filenames. The full existing test suite must still pass.
