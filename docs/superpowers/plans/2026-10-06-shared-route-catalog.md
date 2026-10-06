# Shared Route Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the first visitor calculate a PDF map once, let later visitors open its shared cached routes immediately, reserve recalculation for admins, and give downloads map-specific names.

**Architecture:** A focused `route_catalog.py` owns discovery, legacy fallback, per-map locking, and complete cache publication. `webapp.py` exposes catalog and artifact APIs, checks the PDF hash before rendering, and enforces solve permissions under the map lock. The existing results renderer gains an overview and an authenticated recalculation path.

**Tech Stack:** Python 3.12, FastAPI, local filesystem, POSIX `fcntl`, unittest, vanilla JavaScript, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-06-shared-route-catalog-design.md`

## Global Constraints

- A PDF's SHA-256 content hash identifies its map; a matching filename does not.
- The first visitor may calculate; after publication public callers may only read existing route sets for that map.
- Existing cache directories and their route artifacts remain readable.
- Recalculation requires an authenticated user from `HOFFROUTE_MANUAL_USERS`.
- The catalog never publishes an incomplete route set or removes a working one on failed recalculation.
- Public downloads use the map name, route variant, and format; previews and interactive HTML remain viewable inline.
- Labels are available in English, German, and Romanian.

## Review Focus

- A forged `force` flag or selected cache ID from a public caller must never run the pipeline; Task 3 tests both.
- Two visitors who submit the same uncached PDF at once must produce one published calculation; Task 3 tests the lock and second cache check.
- A malformed or partially written cache directory must not appear in the overview; Task 1 tests missing and invalid summaries and absent artifacts.
- A legacy cache with no matching job metadata must still have a readable, distinct map title; Task 1 tests the summary-name fallback plus hash prefix.
- An artifact request for metadata or a path outside the summary's file list must fail; Task 2 tests it.

---

### Task 1: Catalog, legacy discovery, and atomic publication

**Files:** Create `route_catalog.py`, `tests/test_route_catalog.py`.

**Interfaces:** Produce `list_routes(cache_root: Path, jobs_root: Path, pdf_hash: str | None = None) -> list[dict]`, `get_route(cache_root: Path, jobs_root: Path, route_id: str) -> dict | None`, `publish_route(cache_root: Path, cache_key: str, out_dir: Path, summary: dict, metadata: dict, supersedes: str | None = None) -> str`, and `map_lock(cache_root: Path, pdf_hash: str)` context manager. Catalog records have `id`, `pdf_hash_prefix`, `source_name`, `title`, `created_at`, `start`, `end`, `variants`, `summary`, and `path`; `path` is internal only. New metadata has full `pdf_hash`, `source_name`, `created_at`, `start`, `end`, `calibration_hash`, and optional `supersedes`.

- [ ] Write failing tests: two legacy cache directories for one hash are listed; a matching `jobs/*/meta.json` supplies `source_name`; a cache without job metadata derives title from the first variant and includes its hash prefix; malformed/incomplete directories are omitted; `publish_route` creates a unique complete entry and hides only its `supersedes` entry; failed staging leaves the old entry visible; two threads using `map_lock` on one hash enter serially while different hashes do not block each other.
- [ ] Run `./.venv/bin/python -m unittest -v tests.test_route_catalog`; confirm failures are from missing catalog behavior.
- [ ] Implement the exact interfaces above. Recognize legacy IDs `<20 hex>_<20 hex>` and new IDs with an additional 12 hex characters. Write into a hidden staging directory and rename it to the final ID only after copying listed files, `raw_summary.json`, and `metadata.json`. Use `fcntl.flock` on a per-full-hash file in `.locks`. `list_routes` omits superseded entries, while `get_route` may still serve a valid old ID already open in a browser.
- [ ] Run the focused tests; then commit `route_catalog.py` and its tests.

### Task 2: Direct cached results, early map matching, and named files

**Files:** Modify `webapp.py`, `tests/test_webapp.py`.

**Interfaces:** Consume Task 1 catalog functions. Produce `GET /api/routes` as `{"routes": [...]}` with public list fields only, `GET /api/routes/{route_id}` in the existing result-summary shape, and `GET /api/routes/{route_id}/files/{filename}` with safe inline or attachment headers. Produce `cached_route_response(route: dict) -> dict`. `POST /api/prepare` and `GET /api/prepare/{job_id}/status` may return `{"status":"cached","job_id":...,"routes":[...]}`. Produce authenticated `POST /api/admin/prepare/{job_id}` to render a previously uploaded cached PDF for editing and recalculation.

- [ ] Write failing tests: list and detail show existing cached output without calling `hr.run_pipeline`; a PDF upload with matching hash returns `cached` before `hr.render_page`; URL preparation checks the hash after download and before rendering; a different PDF with the same filename is a miss; file responses have a map-specific `Content-Disposition` filename, with HTML and image previews inline; unlisted files, traversal attempts, and metadata files are rejected; admin prepare rejects a missing cookie and accepts a valid one.
- [ ] Run `./.venv/bin/python -m unittest -v tests.test_webapp`; confirm the new tests fail as expected.
- [ ] Implement those endpoints and the early hash check. Keep the uploaded PDF in its job so the admin can explicitly prepare it for recalculation. Build direct cache URLs into the existing response shape; never copy cached artifacts into a new job. Preserve the existing first-visitor prepare flow on a miss.
- [ ] Run the focused tests; then commit `webapp.py` and `tests/test_webapp.py`.

### Task 3: First-visitor solve and admin-only recalculation

**Files:** Modify `webapp.py`, `tests/test_webapp.py`.

**Interfaces:** `POST /api/solve` accepts existing `job_id`, `calib`, `start`, `end`, plus optional `force: bool` and `supersedes: str`; `force` is authorized only for a logged-in admin. `_run_solve(jid: str, payload: dict, auth_user: str | None)` enters `map_lock` for the PDF hash, checks `list_routes` again, returns a direct cached summary to public callers on a hit, or runs `hr.run_pipeline` and calls `publish_route` on a miss or admin force.

- [ ] Write failing tests: a first public solve publishes one route; a second public solve for the same PDF with different start/end returns cached output and never calls the pipeline; concurrent first solves call the pipeline once; public `force` and public `supersedes` are rejected; an admin's `supersedes` ID must belong to the same map; admin force calls the pipeline and replaces only the selected visible entry; pipeline failure leaves the old route listed and readable.
- [ ] Run `./.venv/bin/python -m unittest -v tests.test_webapp`; confirm the new policy/concurrency tests fail.
- [ ] Implement server-side authorization before job submission and recheck the map cache while holding its lock. Publish the raw summary and artifacts through Task 1; write `solve_result.json` in the existing ready/error shape. A public request ignores its route choices after a cache hit and uses the newest available route set.
- [ ] Run the focused tests; then commit the server behavior and tests.

### Task 4: Public overview and admin controls

**Files:** Modify `static/index.html`, `static/i18n/en.json`, `static/i18n/de.json`, `static/i18n/ro.json`; create `tests/test_route_ui.py`.

**Interfaces:** The page loads `/api/routes` on startup, renders map groups and route-set actions, displays `/api/routes/{id}` through existing `showResults()`, handles `status: cached` after prepare, and offers authenticated `Edit and recalculate` controls that call `/api/admin/prepare/{job_id}` then submit `force: true` with the selected route ID.

- [ ] Write failing UI contract tests for the public overview container, cached-match controls, admin-only recalculation control, and the presence of every new translation key in all three locale files. Parse the HTML returned by `index()` and the locale JSON files so the tests check delivered content rather than source-code fragments.
- [ ] Run `./.venv/bin/python -m unittest -v tests.test_route_ui`; confirm the expected missing UI elements/keys.
- [ ] Implement the UI flow above. Render titles and names with `textContent`; keep route links and filenames from server responses. Hide calculation controls when a cached match is shown to a public user; admins can enter the explicit recalculation flow.
- [ ] Run the focused tests, manually exercise a cached hit and admin path in a browser, then commit the UI and tests.

### Task 5: Persistence, documentation, and final verification

**Files:** Modify `docker-compose.yml`, `README.md`, `tests/test_container.py`.

**Interfaces:** Mount a persistent named volume at `/data/route_cache` in Compose; document how to retain or migrate existing container-local cache data before replacement.

- [ ] Write a failing container test that checks the route-cache volume mapping and a persistent volume declaration.
- [ ] Run `./.venv/bin/python -m unittest -v tests.test_container`; confirm it fails for the missing mount.
- [ ] Add the volume, update the README with the overview, first-visitor policy, admin recalculation, artifact naming, and cache persistence/migration note.
- [ ] Run `./.venv/bin/python -m unittest discover -s tests -v`, `git diff --check`, and `docker compose config` if Docker Compose is available. Review every spec requirement against the changed files and commit the task.
