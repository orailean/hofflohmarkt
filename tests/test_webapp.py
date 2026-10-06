import json
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse

import webapp
import route_catalog
from fastapi import HTTPException, UploadFile
from starlette.requests import Request


TRANSIT_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/aubing_transit_candidates.json")
    .read_text(encoding="utf-8"))


class RouteCacheTests(unittest.TestCase):
    def test_download_names_identify_source_route_and_format(self):
        raw = {
            "variants": [{"key": "circle", "pdf": "route_circle.pdf",
                          "png": "route_circle.png", "gpx": "route_circle.gpx",
                          "kml": "route_circle.kml",
                          "gmaps": "https://www.google.com/maps/dir/?api=1"}],
            "files": ["original.png", "route_circle.pdf",
                      "route_circle.png", "route_circle.gpx",
                      "route_circle.kml", "routes_map.html",
                      "routes.geojson", "google_maps_links.txt"],
        }

        result = webapp.build_response(
            raw, [], "/jobs/123/out",
            source_name="hofflohmaerkte-hombruch-260926.pdf")

        self.assertEqual(result["download_names"], {
            "original.png": "hofflohmaerkte-hombruch-260926-original-preview.png",
            "route_circle.pdf": "hofflohmaerkte-hombruch-260926-circle-route.pdf",
            "route_circle.png": "hofflohmaerkte-hombruch-260926-circle-preview.png",
            "route_circle.gpx": "hofflohmaerkte-hombruch-260926-circle-route.gpx",
            "route_circle.kml": "hofflohmaerkte-hombruch-260926-circle-route.kml",
            "routes_map.html": "hofflohmaerkte-hombruch-260926-interactive-map.html",
            "routes.geojson": "hofflohmaerkte-hombruch-260926-all-routes.geojson",
        })
        self.assertNotIn("google_maps_links.txt", result["files"])
        self.assertEqual(result["variants"][0]["gmaps"],
                         "https://www.google.com/maps/dir/?api=1")
        self.assertEqual(result["variants"][0]["pdf"],
                         "/jobs/123/out/route_circle.pdf")

    def test_generic_upload_name_uses_hash_to_distinguish_downloads(self):
        result = webapp.build_response(
            {"variants": [], "files": ["route_loop.pdf"]}, [],
            source_name="map.pdf", pdf_hash="1234567890abcdef")

        self.assertEqual(result["download_names"]["route_loop.pdf"],
                         "map-12345678-loop-route.pdf")

    def test_cache_key_changes_with_selected_route_endpoints(self):
        calibration = {
            "control_points": [
                {"px": 0, "py": 0, "lat": 48, "lon": 11},
                {"px": 1, "py": 0, "lat": 48, "lon": 12},
                {"px": 0, "py": 1, "lat": 49, "lon": 11},
            ]
        }

        first = webapp.route_cache_dir(
            "abcdef", calibration, start="West", end="East")
        reversed_route = webapp.route_cache_dir(
            "abcdef", calibration, start="East", end="West")

        self.assertNotEqual(first, reversed_route)

    def test_route_cache_version_invalidates_pre_directional_results(self):
        calibration = {"control_points": []}
        current = webapp.route_cache_dir("abcdef", calibration)

        with mock.patch.object(webapp, "ROUTE_CACHE_VERSION", "street-v2"):
            legacy = webapp.route_cache_dir("abcdef", calibration)

        self.assertEqual(webapp.ROUTE_CACHE_VERSION, "flyer-streets-v5")
        self.assertNotEqual(current, legacy)

    def test_calibration_cache_version_invalidates_old_station_names(self):
        current = webapp.cache_path("abcdef")
        legacy = webapp.CALIB_CACHE_DIR / "abcdef.json"

        self.assertEqual(webapp.CALIB_CACHE_VERSION, "station-resolver-v2")
        self.assertNotEqual(current, legacy)

    def test_result_distinguishes_flyer_and_gps_route_status(self):
        summary = {
            "flyer_route": {"available": True},
            "gps_route": {
                "available": False,
                "warning": "GPS export unavailable: calibration is unreliable",
            },
            "variants": [],
            "files": [],
        }

        response = webapp.build_response(summary, [])

        self.assertTrue(response["flyer_route"]["available"])
        self.assertFalse(response["gps_route"]["available"])
        self.assertIn(
            "calibration", response["gps_route"]["warning"].lower())


class RouteApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.jobs = root / "jobs"
        self.cache = root / "routes"
        self.jobs.mkdir()
        self.cache.mkdir()
        self.jobs_patch = mock.patch.object(webapp, "JOBS_DIR", self.jobs)
        self.cache_patch = mock.patch.object(webapp, "ROUTE_CACHE_DIR", self.cache)
        self.jobs_patch.start()
        self.cache_patch.start()
        self.addCleanup(self.jobs_patch.stop)
        self.addCleanup(self.cache_patch.stop)
        self.pdf = b"%PDF-1.4\nmap-one"
        self.pdf_hash = hashlib.sha256(self.pdf).hexdigest()

    def request(self, admin=False):
        cookie = (f"{webapp.AUTH_COOKIE}={webapp.make_auth_token('admin')}"
                  if admin else "")
        return Request({"type": "http", "headers":
                        [(b"cookie", cookie.encode())] if cookie else []})

    def cached_route(self):
        output = Path(self.tmp.name) / "output"
        output.mkdir(exist_ok=True)
        (output / "route_circle.pdf").write_bytes(b"%PDF-route")
        (output / "route_circle.png").write_bytes(b"preview")
        (output / "original.png").write_bytes(b"flyer")
        (output / "routes_map.html").write_text("<html>map</html>")
        summary = {"dots": 3, "variants": [{
            "key": "circle", "name": "Aubing circle", "pdf": "route_circle.pdf",
            "png": "route_circle.png", "gpx": None, "kml": None}],
            "files": ["original.png", "route_circle.pdf", "route_circle.png",
                      "routes_map.html"]}
        key = self.pdf_hash[:20] + "_" + "1" * 20
        return route_catalog.publish_route(self.cache, key, output, summary, {
            "pdf_hash": self.pdf_hash, "source_name": "hofflohmaerkte-aubing.pdf",
            "created_at": "2026-10-06T12:00:00Z", "start": None,
            "end": None, "calibration_hash": "nocal"})

    def make_job(self):
        jid = "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + str(len(list(self.jobs.iterdir()))).zfill(12)
        d = self.jobs / jid
        d.mkdir()
        (d / "map.pdf").write_bytes(self.pdf)
        (d / "meta.json").write_text(json.dumps({
            "pdf_hash": self.pdf_hash, "pdf_name": "map.pdf",
            "source_name": "hofflohmaerkte-aubing.pdf"}))
        return jid

    @staticmethod
    def fake_pipeline(pdf, calib, out, **kwargs):
        out.mkdir(parents=True, exist_ok=True)
        (out / "route_circle.pdf").write_bytes(b"%PDF-computed")
        (out / "route_circle.png").write_bytes(b"preview")
        (out / "original.png").write_bytes(b"flyer")
        return {"dots": 3, "variants": [{
            "key": "circle", "name": "Aubing circle", "pdf": "route_circle.pdf",
            "png": "route_circle.png", "gpx": None, "kml": None}],
            "files": ["original.png", "route_circle.pdf", "route_circle.png"]}

    def test_catalog_result_uses_shared_files_without_pipeline(self):
        route_id = self.cached_route()
        with mock.patch.object(webapp.hr, "run_pipeline",
                               side_effect=AssertionError("pipeline ran")):
            overview = webapp.routes_list()
            result = webapp.route_result(route_id)
        self.assertEqual(overview["routes"][0]["id"], route_id)
        self.assertNotIn("path", overview["routes"][0])
        self.assertNotIn("summary", overview["routes"][0])
        self.assertEqual(result["variants"][0]["pdf"],
                         f"/api/routes/{route_id}/files/route_circle.pdf")
        self.assertEqual(result["status"], "ready")

    def test_matching_upload_returns_cached_before_rendering(self):
        route_id = self.cached_route()
        upload = UploadFile(filename="another-name.pdf", file=io.BytesIO(self.pdf))
        with mock.patch.object(webapp.hr, "render_page",
                               side_effect=AssertionError("rendered")), \
             mock.patch.object(webapp._prepare_executor, "submit") as submit:
            response = webapp.prepare(file=upload, url=None)
        self.assertEqual(response["status"], "cached")
        self.assertEqual(response["routes"][0]["id"], route_id)
        submit.assert_not_called()

    def test_same_filename_with_different_pdf_is_not_a_cache_hit(self):
        self.cached_route()
        upload = UploadFile(filename="hofflohmaerkte-aubing.pdf",
                            file=io.BytesIO(b"%PDF-1.4\nother map"))
        with mock.patch.object(webapp._prepare_executor, "submit"):
            response = webapp.prepare(file=upload, url=None)
        self.assertEqual(response["status"], "processing")

    def test_url_prepare_checks_cache_before_render(self):
        route_id = self.cached_route()
        jid = "a" * 36
        (self.jobs / jid).mkdir()

        def download(_url, dest):
            pdf = dest / "map.pdf"
            pdf.write_bytes(self.pdf)
            return pdf

        with mock.patch.object(webapp.hr, "fetch_pdf", side_effect=download), \
             mock.patch.object(webapp.hr, "render_page",
                               side_effect=AssertionError("rendered")):
            webapp._run_prepare(jid, None, "https://example.test/map.pdf")
        result = json.loads((self.jobs / jid / "result.json").read_text())
        self.assertEqual(result["status"], "cached")
        self.assertEqual(result["routes"][0]["id"], route_id)

    def test_artifacts_have_map_specific_names_and_private_metadata_is_blocked(self):
        route_id = self.cached_route()
        pdf = webapp.route_file(route_id, "route_circle.pdf")
        preview = webapp.route_file(route_id, "route_circle.png")
        self.assertIn("hofflohmaerkte-aubing-circle-route.pdf",
                      pdf.headers["content-disposition"])
        self.assertIn("inline", preview.headers["content-disposition"])
        for filename in ("metadata.json", "raw_summary.json", "../meta.json"):
            with self.assertRaises(HTTPException):
                webapp.route_file(route_id, filename)

    def test_admin_prepare_requires_login(self):
        upload = UploadFile(filename="map.pdf", file=io.BytesIO(self.pdf))
        with mock.patch.object(webapp._prepare_executor, "submit"):
            jid = webapp.prepare(file=upload, url=None)["job_id"]
        with self.assertRaises(HTTPException) as error:
            webapp.admin_prepare(jid, self.request())
        self.assertEqual(error.exception.status_code, 403)
        with mock.patch.object(webapp, "MANUAL_USERS", {"admin": "pw"}), \
             mock.patch.object(webapp._prepare_executor, "submit") as submit:
            response = webapp.admin_prepare(jid, self.request(admin=True))
        self.assertEqual(response["status"], "processing")
        submit.assert_called_once()

    def test_first_public_solve_publishes_and_later_options_reuse_it(self):
        first = self.make_job()
        second = self.make_job()
        with mock.patch.object(webapp.hr, "run_pipeline",
                               side_effect=self.fake_pipeline) as pipeline:
            webapp._run_solve(first, {"start": "West"}, None)
            webapp._run_solve(second, {"start": "East"}, None)
        self.assertEqual(pipeline.call_count, 1)
        first_result = json.loads((self.jobs / first / "solve_result.json").read_text())
        second_result = json.loads((self.jobs / second / "solve_result.json").read_text())
        self.assertEqual(first_result["status"], "ready")
        self.assertEqual(second_result["cache_id"], first_result["cache_id"])
        self.assertTrue(first_result["base"].startswith("/api/routes/"))

    def test_concurrent_first_solves_compute_once(self):
        first = self.make_job()
        second = self.make_job()
        calls = []

        def slow_pipeline(*args, **kwargs):
            calls.append(1)
            time.sleep(0.08)
            return self.fake_pipeline(*args, **kwargs)

        with mock.patch.object(webapp.hr, "run_pipeline", side_effect=slow_pipeline):
            threads = [threading.Thread(target=webapp._run_solve,
                        args=(jid, {}, None)) for jid in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
        self.assertEqual(len(calls), 1)
        results = [json.loads((self.jobs / jid / "solve_result.json").read_text())
                   for jid in (first, second)]
        self.assertEqual([r["status"] for r in results], ["ready", "ready"])
        self.assertEqual(results[0]["cache_id"], results[1]["cache_id"])

    def test_public_cannot_force_or_supersede(self):
        jid = self.make_job()
        for payload in ({"job_id": jid, "force": True},
                        {"job_id": jid, "supersedes": "x"}):
            with self.assertRaises(HTTPException) as error:
                webapp.solve(payload, self.request())
            self.assertEqual(error.exception.status_code, 403)

    def test_admin_cannot_supersede_another_map(self):
        jid = self.make_job()
        other_id = self.cached_route()
        (self.jobs / jid / "meta.json").write_text(json.dumps({
            "pdf_hash": "f" * 64, "source_name": "other.pdf"}))
        with mock.patch.object(webapp, "MANUAL_USERS", {"admin": "pw"}):
            with self.assertRaises(HTTPException) as error:
                webapp.solve({"job_id": jid, "force": True,
                              "supersedes": other_id}, self.request(admin=True))
        self.assertEqual(error.exception.status_code, 400)

    def test_admin_rebuild_replaces_selected_entry_after_success(self):
        old_id = self.cached_route()
        jid = self.make_job()
        with mock.patch.object(webapp.hr, "run_pipeline",
                               side_effect=self.fake_pipeline):
            webapp._run_solve(jid, {"force": True, "supersedes": old_id},
                              "admin")
        result = json.loads((self.jobs / jid / "solve_result.json").read_text())
        self.assertEqual(result["status"], "ready")
        self.assertNotEqual(result["cache_id"], old_id)
        self.assertEqual([r["id"] for r in route_catalog.list_routes(
            self.cache, self.jobs, self.pdf_hash)], [result["cache_id"]])
        self.assertIsNotNone(route_catalog.get_route(self.cache, self.jobs, old_id))

    def test_failed_admin_rebuild_keeps_old_route_visible(self):
        old_id = self.cached_route()
        jid = self.make_job()
        with mock.patch.object(webapp.hr, "run_pipeline",
                               side_effect=RuntimeError("routing unavailable")):
            webapp._run_solve(jid, {"force": True, "supersedes": old_id},
                              "admin")
        result = json.loads((self.jobs / jid / "solve_result.json").read_text())
        self.assertEqual(result["status"], "error")
        self.assertEqual([r["id"] for r in route_catalog.list_routes(
            self.cache, self.jobs, self.pdf_hash)], [old_id])


class TransitProviderTests(unittest.TestCase):
    def test_overpass_query_uses_one_expanded_district_bounding_box(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"elements":[]}'

        with mock.patch(
            "webapp.urllib.request.urlopen", return_value=response
        ) as open_url:
            webapp.overpass_transit_candidates(
                "Aubing, München", TRANSIT_FIXTURE["control_points"],
                TRANSIT_FIXTURE["icons"],
            )

        request = open_url.call_args.args[0]
        query = urllib.parse.parse_qs(request.data.decode())["data"][0]
        self.assertNotIn("around:", query)
        self.assertNotIn("body 100", query)
        self.assertRegex(
            query,
            r'node\(48\.12\d+,11\.3\d+,48\.18\d+,11\.4\d+\)',
        )

    def test_overpass_query_retries_a_second_free_endpoint(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "elements": TRANSIT_FIXTURE["elements"],
        }).encode()

        with mock.patch.object(
            webapp,
            "OVERPASS_URLS",
            ("https://primary.example/interpreter",
             "https://fallback.example/interpreter"),
        ), mock.patch(
            "webapp.urllib.request.urlopen",
            side_effect=[TimeoutError("primary timed out"), response],
        ) as open_url:
            candidates = webapp.overpass_transit_candidates(
                "Aubing, München", TRANSIT_FIXTURE["control_points"],
                TRANSIT_FIXTURE["icons"],
            )

        self.assertEqual(candidates, TRANSIT_FIXTURE["elements"])
        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(
            [call.args[0].full_url for call in open_url.call_args_list],
            ["https://primary.example/interpreter",
             "https://fallback.example/interpreter"],
        )

    def test_transit_resolution_uses_nominatim_after_overpass_failure(self):
        with mock.patch(
            "webapp.overpass_transit_candidates",
            side_effect=TimeoutError("Overpass timed out"),
        ), mock.patch(
            "webapp.nominatim_transit_candidates",
            return_value=TRANSIT_FIXTURE["elements"],
        ):
            result = webapp.transit_stations_from_icons(
                TRANSIT_FIXTURE["icons"],
                TRANSIT_FIXTURE["control_points"],
                "Aubing, München",
            )

        self.assertEqual(
            [station["name"] for station in result.stations],
            ["Aubing", "Leienfelsstraße"],
        )
        self.assertEqual(result.warnings, [])

    def test_nominatim_searches_station_types_inside_control_bounds(self):
        def response(results):
            value = mock.MagicMock()
            value.__enter__.return_value.read.return_value = json.dumps(
                results).encode()
            return value

        aubing, leienfels = TRANSIT_FIXTURE["elements"][:2]

        def nominatim_result(element):
            return {
                "osm_type": element["type"],
                "osm_id": element["id"],
                "lat": str(element["lat"]),
                "lon": str(element["lon"]),
                "category": "railway",
                "type": element["tags"]["railway"],
                "name": element["tags"]["name"],
                "display_name": element["tags"]["name"],
            }

        with mock.patch(
            "webapp.urllib.request.urlopen",
            side_effect=[
                response([]),
                response([nominatim_result(leienfels)]),
                response([nominatim_result(aubing)]),
            ],
        ) as open_url, mock.patch("webapp.time.sleep"):
            candidates = webapp.nominatim_transit_candidates(
                "Aubing, München", TRANSIT_FIXTURE["control_points"],
                TRANSIT_FIXTURE["icons"],
            )

        self.assertEqual(
            {item["tags"]["name"] for item in candidates},
            {"Aubing", "Leienfelsstraße"},
        )
        queries = [
            urllib.parse.parse_qs(
                urllib.parse.urlparse(call.args[0].full_url).query)
            for call in open_url.call_args_list
        ]
        self.assertEqual(
            [query["q"][0] for query in queries],
            ["S-Bahn Aubing, München", "Bahnhof", "Haltepunkt"],
        )
        self.assertTrue(all("viewbox" in query and query["bounded"] == ["1"]
                            for query in queries[1:]))

    def test_auto_calibration_persists_station_names_and_warnings(self):
        candidates = [
            {"name": "One", "px": 10, "py": 10},
            {"name": "Two", "px": 20, "py": 10},
            {"name": "Three", "px": 10, "py": 20},
        ]
        geocoded = [(48.0, 11.0), (48.0, 11.1), (48.1, 11.0)]
        resolution = webapp.station_names.StationResolution(
            stations=[{
                "name": "Aubing", "mode": "S", "px": 10.0, "py": 10.0,
                "lat": 48.1559713, "lon": 11.4131591,
                "osm_id": "node/2488173642",
            }],
            warnings=["Station name unavailable for one transit icon"],
        )

        with mock.patch("webapp.extract_text_lines", return_value=[]), \
             mock.patch("webapp.detect_autocalib_context",
                        return_value="Aubing, München"), \
             mock.patch("webapp.map_label_candidates",
                        return_value=candidates), \
             mock.patch("webapp.geocode_one", side_effect=geocoded), \
             mock.patch("webapp.fit_auto_points", side_effect=lambda p: p), \
             mock.patch("webapp.transit_stations_from_icons",
                        return_value=resolution):
            calibration = webapp.auto_calibrate(
                Path("map.pdf"), 300, Path("map.png"),
                TRANSIT_FIXTURE["icons"], [(20, 20)], 400, 300,
            )

        self.assertEqual(calibration["stations"][0]["name"], "Aubing")
        self.assertEqual(calibration["station_warnings"], resolution.warnings)
        self.assertEqual(calibration["context"], "Aubing, München")


if __name__ == "__main__":
    unittest.main()
