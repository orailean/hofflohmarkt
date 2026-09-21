import unittest
from unittest import mock
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import fitz
import numpy as np

import hoffroute as hr


PINK = np.array([210, 20, 100])


def draw_disc(image, x, y, radius=10):
    yy, xx = np.ogrid[:image.shape[0], :image.shape[1]]
    image[(xx - x) ** 2 + (yy - y) ** 2 <= radius ** 2] = PINK


class DotDetectionTests(unittest.TestCase):
    def test_rejects_legend_marker_even_when_it_is_not_an_outlier(self):
        image = np.full((500, 800, 3), 255, dtype=np.uint8)
        real_dots = [(120, 300), (230, 300), (340, 300), (450, 300)]
        for x, y in real_dots:
            draw_disc(image, x, y)

        legend = (560, 230)
        draw_disc(image, *legend)
        # A compact group of magenta text-like strokes around the sample dot.
        for row in range(4):
            for col in range(6):
                x = 615 + col * 18
                y = 190 + row * 18
                image[y:y + 4, x:x + 10] = PINK

        detected = hr.detect_dots(image, (0, 0, 800, 500))

        self.assertEqual(len(detected), len(real_dots))
        self.assertTrue(all(
            any(np.hypot(px - x, py - y) < 3 for px, py in detected)
            for x, y in real_dots
        ))
        self.assertFalse(any(
            np.hypot(px - legend[0], py - legend[1]) < 20
            for px, py in detected
        ))


class GeoreferencingTests(unittest.TestCase):
    def test_reverse_affine_projects_route_geometry_back_to_map_pixels(self):
        control_points = [
            {"px": 0, "py": 0, "lat": 48, "lon": 11},
            {"px": 100, "py": 0, "lat": 48, "lon": 12},
            {"px": 0, "py": 200, "lat": 50, "lon": 11},
        ]

        ll2px = hr.fit_reverse_affine(control_points)

        x, y = ll2px(49, 11.5)
        self.assertAlmostEqual(x, 50)
        self.assertAlmostEqual(y, 100)


class StreetRouterTests(unittest.TestCase):
    def setUp(self):
        hr.StreetRouter._last_request_at = None

    def test_distance_matrix_returns_walking_distances(self):
        router = hr.StreetRouter(min_interval=0)
        response = {
            "code": "Ok",
            "distances": [
                [0, 120, 340],
                [121, 0, 230],
                [342, 229, 0],
            ],
            "sources": [{"location": [11, 48]}] * 3,
            "destinations": [{"location": [11, 48]}] * 3,
        }

        with mock.patch.object(router, "_request_json", return_value=response):
            distances = router.distance_matrix([
                (48.0, 11.0), (48.001, 11.001), (48.002, 11.002)
            ])

        np.testing.assert_array_equal(distances, np.array(response["distances"]))

    def test_route_failure_does_not_fall_back_to_straight_lines(self):
        router = hr.StreetRouter(min_interval=0)

        with mock.patch.object(
            router, "_request_json", return_value={
                "code": "NoRoute", "message": "No route found"
            }
        ):
            with self.assertRaisesRegex(hr.StreetRoutingError, "No route found"):
                router.route([(48.0, 11.0), (48.001, 11.001)])

    def test_route_returns_street_geometry_distance_and_duration(self):
        router = hr.StreetRouter(min_interval=0)
        response = {
            "code": "Ok",
            "routes": [{
                "distance": 420.5,
                "duration": 310.0,
                "geometry": {"type": "LineString", "coordinates": [
                    [11.0, 48.0], [11.0005, 48.001], [11.001, 48.001]
                ]},
                "legs": [],
            }],
            "waypoints": [
                {"location": [11.0, 48.0], "name": ""},
                {"location": [11.001, 48.001], "name": ""},
            ],
        }

        with mock.patch.object(router, "_request_json", return_value=response):
            geometry, distance, duration = router.route(
                [(48.0, 11.0), (48.001, 11.001)]
            )

        self.assertEqual(
            geometry,
            [(48.0, 11.0), (48.001, 11.0005), (48.001, 11.001)],
        )
        self.assertEqual(distance, 420.5)
        self.assertEqual(duration, 310.0)

    def test_distance_matrix_is_tiled_for_large_stop_sets(self):
        router = hr.StreetRouter(min_interval=0, table_block=2)
        coords = [(float(i), float(i)) for i in range(4)]

        def table_response(service, request_coords, query):
            self.assertEqual(service, "table")
            source_positions = [int(i) for i in query["sources"].split(";")]
            destination_positions = [
                int(i) for i in query["destinations"].split(";")
            ]
            source_ids = [int(request_coords[i][0]) for i in source_positions]
            destination_ids = [
                int(request_coords[i][0]) for i in destination_positions
            ]
            return {
                "code": "Ok",
                "distances": [
                    [100 * source + destination for destination in destination_ids]
                    for source in source_ids
                ],
                "sources": [{"location": [0, 0]} for _ in source_ids],
                "destinations": [
                    {"location": [0, 0]} for _ in destination_ids
                ],
            }

        with mock.patch.object(router, "_request_json", side_effect=table_response):
            distances = router.distance_matrix(coords)

        np.testing.assert_array_equal(distances, np.array([
            [0, 1, 2, 3],
            [100, 101, 102, 103],
            [200, 201, 202, 203],
            [300, 301, 302, 303],
        ]))

    def test_route_chunks_are_joined_without_duplicate_points(self):
        router = hr.StreetRouter(min_interval=0, route_chunk=2)
        coords = [(48.0, 11.0), (48.1, 11.1), (48.2, 11.2), (48.3, 11.3)]

        def route_response(service, request_coords, query):
            self.assertEqual(service, "route")
            return {
                "code": "Ok",
                "routes": [{
                    "distance": 100.0,
                    "duration": 80.0,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [lon, lat] for lat, lon in request_coords
                        ],
                    },
                    "legs": [],
                }],
                "waypoints": [
                    {"location": [lon, lat], "name": ""}
                    for lat, lon in request_coords
                ],
            }

        with mock.patch.object(router, "_request_json", side_effect=route_response):
            geometry, distance, duration = router.route(coords)

        self.assertEqual(geometry, coords)
        self.assertEqual(distance, 200.0)
        self.assertEqual(duration, 160.0)

    def test_separate_router_instances_share_the_service_rate_limit(self):
        first = hr.StreetRouter(min_interval=1)
        second = hr.StreetRouter(min_interval=1)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"code":"Ok"}'

        with mock.patch("hoffroute.urllib.request.urlopen", return_value=response), \
             mock.patch("hoffroute.time.monotonic", side_effect=[0.0, 0.25, 1.0]), \
             mock.patch("hoffroute.time.sleep") as sleep:
            first._request_json("test", [(48.0, 11.0)], {})
            second._request_json("test", [(48.0, 11.0)], {})

        sleep.assert_called_once_with(0.75)


class FakeStreetRouter:
    def __init__(self):
        self.matrix_coords = None

    def distance_matrix(self, coords):
        self.matrix_coords = list(coords)
        expensive = 1000.0
        distances = np.full((4, 4), expensive)
        np.fill_diagonal(distances, 0)
        for a, b in ((0, 2), (2, 1), (1, 3), (3, 0)):
            distances[a, b] = distances[b, a] = 10
        return distances

    def route(self, coords):
        # Include a visible detour that cannot occur in a straight stop-to-stop line.
        geometry = [coords[0], (48.03, 11.03), *coords[1:]]
        return geometry, 400.0, 300.0


class PipelineTests(unittest.TestCase):
    def _write_map_pdf(self, path):
        document = fitz.open()
        page = document.new_page(width=600, height=600)
        roads = page.new_shape()
        roads.draw_polyline([
            fitz.Point(114, 114), fitz.Point(185, 114),
            fitz.Point(185, 185), fitz.Point(114, 185),
            fitz.Point(114, 114),
        ])
        roads.finish(color=(190 / 255, 218 / 255, 247 / 255), width=7)
        roads.commit()
        shape = page.new_shape()
        for x, y in ((100, 100), (200, 100), (200, 200), (100, 200)):
            shape.draw_circle(fitz.Point(x, y), 10)
        pink = tuple(PINK / 255)
        shape.finish(color=pink, fill=pink)
        shape.commit()
        document.save(path)
        document.close()

    def _calibration(self):
        return {
            "map_bbox_px": [0, 0, 600, 600],
            "control_points": [
                {"px": 0, "py": 0, "lat": 48, "lon": 11},
                {"px": 600, "py": 0, "lat": 48, "lon": 11.06},
                {"px": 0, "py": 600, "lat": 48.06, "lon": 11},
            ],
            "stations": [],
        }

    def test_pipeline_without_georeferencing_still_writes_flyer_route(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            pdf = temp_path / "map.pdf"
            output = temp_path / "out"
            self._write_map_pdf(pdf)

            summary = hr.run_pipeline(
                pdf, None, output, dpi=72, log=lambda _message: None)

            self.assertFalse(summary["gps_available"])
            self.assertTrue((output / "route_circle.pdf").is_file())
            self.assertFalse((output / "route_circle.gpx").exists())

    def test_pipeline_draws_closed_flyer_graph_route_with_every_access_spur(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            pdf = temp_path / "map.pdf"
            self._write_map_pdf(pdf)
            output = temp_path / "out"

            summary = hr.run_pipeline(
                pdf, self._calibration(), output, dpi=72,
                street_router=FakeStreetRouter(),
                log=lambda _message: None,
            )
            circle = next(v for v in summary["variants"]
                          if v["key"] == "circle")
            self.assertTrue(circle["closed"])
            self.assertEqual(circle["unique_stops"], 4)
            self.assertEqual(circle["access_spurs"], 4)
            self.assertLessEqual(circle["max_road_offset_px"], 2 * 72 / 300)

            document = fitz.open(output / "route_circle.pdf")
            route_drawings = [
                drawing for drawing in document[0].get_drawings()
                if drawing["color"] is not None
                and np.allclose(drawing["color"], (0.11, 0.46, 0.84))
            ]
            route_bounds = max(
                (drawing["rect"] for drawing in route_drawings),
                key=lambda rect: rect.get_area(),
            )
            document.close()

        self.assertGreaterEqual(len(route_drawings), 2)
        self.assertLess(route_bounds.x1, 250)
        self.assertLess(route_bounds.y1, 250)

    def test_station_callout_names_the_station_and_its_route_role(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.pdf"
            output = Path(temp) / "annotated.pdf"
            document = fitz.open()
            document.new_page(width=600, height=600)
            document.save(source)
            document.close()

            hr.annotate_pdf(
                source, output, [(100, 100), (200, 200)], "Test route",
                dpi=72,
                station_labels=[{
                    "x": 100,
                    "y": 100,
                    "name": "Aubing Bahnhof",
                    "kind": "S",
                    "role": "start",
                }],
            )

            document = fitz.open(output)
            spans = [
                span
                for block in document[0].get_text("dict")["blocks"]
                if "lines" in block
                for line in block["lines"]
                for span in line["spans"]
            ]
            document.close()

        text = " ".join(span["text"] for span in spans)
        self.assertIn("START", text)
        self.assertIn("S-BAHN", text)
        self.assertIn("Aubing Bahnhof", text)
        station_span = next(
            span for span in spans if "Aubing Bahnhof" in span["text"]
        )
        self.assertGreaterEqual(station_span["size"], 8)


if __name__ == "__main__":
    unittest.main()
