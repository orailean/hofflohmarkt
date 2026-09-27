import json
from pathlib import Path
import unittest

from station_resolver import resolve_station_icons


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/aubing_transit_candidates.json")
    .read_text(encoding="utf-8"))


class StationResolverTests(unittest.TestCase):
    def test_rejects_distant_stations_even_when_their_layout_matches(self):
        icons = [("U-Bahn (blue icon)", 0, 0),
                 ("U-Bahn (blue icon)", 0, 1000)]
        controls = [
            {"px": 0, "py": 0, "lat": 48.120, "lon": 11.575},
            {"px": 1000, "py": 0, "lat": 48.120, "lon": 11.585},
            {"px": 0, "py": 1000, "lat": 48.113, "lon": 11.575},
        ]
        distant = [
            {"type": "node", "id": 1, "lat": 48.140, "lon": 11.575,
             "tags": {"name": "Marienplatz", "station": "subway"}},
            {"type": "node", "id": 2, "lat": 48.133, "lon": 11.575,
             "tags": {"name": "Baierbrunner Straße", "station": "subway"}},
        ]

        result = resolve_station_icons(
            icons, controls, "Untergiesing, München",
            candidate_providers=[lambda *_args: distant])

        self.assertEqual(result.stations, [])
        self.assertTrue(result.warnings)

    def test_prefers_stations_close_to_each_icon(self):
        icons = [("U-Bahn (blue icon)", 0, 0),
                 ("U-Bahn (blue icon)", 0, 1000)]
        controls = [
            {"px": 0, "py": 0, "lat": 48.120, "lon": 11.575},
            {"px": 1000, "py": 0, "lat": 48.120, "lon": 11.585},
            {"px": 0, "py": 1000, "lat": 48.113, "lon": 11.575},
        ]
        elements = [
            {"type": "node", "id": 1, "lat": 48.140, "lon": 11.575,
             "tags": {"name": "Marienplatz", "station": "subway"}},
            {"type": "node", "id": 2, "lat": 48.133, "lon": 11.575,
             "tags": {"name": "Baierbrunner Straße", "station": "subway"}},
            {"type": "node", "id": 3, "lat": 48.1198, "lon": 11.5766,
             "tags": {"name": "Kolumbusplatz", "station": "subway"}},
            {"type": "node", "id": 4, "lat": 48.1131, "lon": 11.5717,
             "tags": {"name": "Candidplatz", "station": "subway"}},
        ]

        result = resolve_station_icons(
            icons, controls, "Untergiesing, München",
            candidate_providers=[lambda *_args: elements])

        self.assertEqual([s["name"] for s in result.stations],
                         ["Kolumbusplatz", "Candidplatz"])

    def test_matches_aubing_icons_jointly_to_real_unique_station_names(self):
        result = resolve_station_icons(
            FIXTURE["icons"], FIXTURE["control_points"], "Aubing, München",
            candidate_providers=[lambda *_args: FIXTURE["elements"]],
        )

        self.assertEqual(
            [station["name"] for station in result.stations],
            ["Aubing", "Leienfelsstraße"],
        )
        self.assertEqual(
            len({station["osm_id"] for station in result.stations}), 2)
        self.assertEqual(result.warnings, [])

    def test_uses_second_provider_after_overpass_timeout(self):
        def raising_timeout(*_args):
            raise TimeoutError("Overpass timed out")

        result = resolve_station_icons(
            FIXTURE["icons"], FIXTURE["control_points"], "Aubing, München",
            candidate_providers=[
                raising_timeout, lambda *_args: FIXTURE["elements"]
            ],
        )

        self.assertEqual(len(result.stations), 2)

    def test_uses_second_provider_when_first_candidates_do_not_resolve(self):
        result = resolve_station_icons(
            FIXTURE["icons"], FIXTURE["control_points"], "Aubing, München",
            candidate_providers=[
                lambda *_args: FIXTURE["elements"][:1],
                lambda *_args: FIXTURE["elements"],
            ],
        )

        self.assertEqual(
            [station["name"] for station in result.stations],
            ["Aubing", "Leienfelsstraße"],
        )
        self.assertEqual(result.warnings, [])

    def test_unresolved_icon_gets_warning_not_generic_station_name(self):
        result = resolve_station_icons(
            FIXTURE["icons"][:1], FIXTURE["control_points"],
            "Aubing, München", candidate_providers=[lambda *_args: []],
        )

        self.assertEqual(result.stations, [])
        self.assertIn("Station name unavailable", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
