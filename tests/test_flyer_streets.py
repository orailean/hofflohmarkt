import unittest

import numpy as np

from flyer_streets import FlyerStreetError, FlyerStreetGraph


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


def graph_fixture(shape=(200, 200), cross=True):
    mask = np.zeros(shape, dtype=bool)
    mask[50, 20:181] = True
    if cross:
        mask[20:101, 100] = True
    return FlyerStreetGraph.from_skeleton(mask.copy(), mask, dpi=300)


class FlyerStreetExtractionTests(unittest.TestCase):
    def test_extracts_roads_closes_label_gap_and_discards_decoration(self):
        image = synthetic_street_map(label_gap=6, block_gap=None)

        graph = FlyerStreetGraph.from_image(
            image, (20, 20, 380, 260), dpi=300)

        self.assertTrue(graph.road_mask[100, 80])
        self.assertFalse(graph.road_mask[100, 200])
        self.assertTrue(graph.connected((80, 100), (320, 100)))
        self.assertFalse(graph.road_mask[280, 200])

    def test_does_not_bridge_a_wide_gap_across_a_block(self):
        image = synthetic_street_map(label_gap=None, block_gap=40)

        graph = FlyerStreetGraph.from_image(
            image, (20, 20, 380, 260), dpi=300)

        self.assertFalse(graph.connected((80, 100), (320, 100)))


class FlyerStreetRoutingTests(unittest.TestCase):
    def test_snap_stops_routes_every_stop_and_closes_on_street_mask(self):
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

        access = graph.snap_stops(
            [(40, 45), (40, 55)], max_distance_px=15)

        self.assertEqual(access[0].node, access[1].node)
        self.assertEqual(graph.distance_matrix(access)[0, 1], 0)

    def test_snap_stops_ignores_a_closer_tiny_disconnected_artifact(self):
        mask = np.zeros((100, 200), dtype=bool)
        mask[50, 20:181] = True
        mask[43, 23:30] = True
        graph = FlyerStreetGraph.from_skeleton(mask.copy(), mask, dpi=300)

        access = graph.snap_stops(
            [(25, 42), (150, 55)], max_distance_px=15)

        self.assertEqual([item.street_xy[1] for item in access], [50, 50])
        self.assertTrue(np.isfinite(graph.distance_matrix(access)).all())

    def test_reports_number_of_an_unreachable_market_marker(self):
        with self.assertRaisesRegex(FlyerStreetError, "marker 2"):
            graph_fixture(cross=False).snap_stops(
                [(40, 45), (180, 180)], max_distance_px=15)


if __name__ == "__main__":
    unittest.main()
