import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.parse

import webapp


TRANSIT_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/aubing_transit_candidates.json")
    .read_text(encoding="utf-8"))


class RouteCacheTests(unittest.TestCase):
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

        self.assertEqual(webapp.ROUTE_CACHE_VERSION, "flyer-streets-v2")
        self.assertNotEqual(current, legacy)

    def test_calibration_cache_version_invalidates_old_station_names(self):
        current = webapp.cache_path("abcdef")
        legacy = webapp.CALIB_CACHE_DIR / "abcdef.json"

        self.assertEqual(webapp.CALIB_CACHE_VERSION, "station-resolver-v1")
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
