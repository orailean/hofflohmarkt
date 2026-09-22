import json
from pathlib import Path
import unittest

from station_resolver import resolve_station_icons


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/aubing_transit_candidates.json")
    .read_text(encoding="utf-8"))


class StationResolverTests(unittest.TestCase):
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
