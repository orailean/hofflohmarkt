import unittest

import numpy as np

from flyer_streets import FlyerStreetGraph


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


if __name__ == "__main__":
    unittest.main()
