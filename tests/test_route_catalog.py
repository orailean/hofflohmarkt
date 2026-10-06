import importlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path


try:
    route_catalog = importlib.import_module("route_catalog")
except ImportError:
    route_catalog = None


HASH = "a" * 64
OLD_ID = "a" * 20 + "_" + "b" * 20


class RouteCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache = root / "cache"
        self.jobs = root / "jobs"
        self.cache.mkdir()
        self.jobs.mkdir()
        self.assertIsNotNone(route_catalog, "route_catalog module is missing")

    def make_route(self, route_id=OLD_ID, name="Aubing circuit"):
        d = self.cache / route_id
        d.mkdir()
        (d / "route_circle.pdf").write_bytes(b"%PDF-route")
        (d / "route_circle.png").write_bytes(b"image")
        (d / "original.png").write_bytes(b"flyer")
        (d / "raw_summary.json").write_text(json.dumps({
            "dots": 4,
            "variants": [{"key": "circle", "name": name,
                          "pdf": "route_circle.pdf", "png": "route_circle.png"}],
            "files": ["original.png", "route_circle.pdf", "route_circle.png"],
        }))
        return d

    def test_legacy_entries_use_job_name_and_summary_fallback(self):
        self.make_route()
        second_id = "a" * 20 + "_" + "c" * 20
        self.make_route(second_id, "Other circuit")
        job = self.jobs / "some-job"
        job.mkdir()
        (job / "meta.json").write_text(json.dumps({
            "pdf_hash": HASH, "source_name": "aubing-market.pdf"}))

        routes = route_catalog.list_routes(self.cache, self.jobs, HASH)
        self.assertEqual({r["id"] for r in routes}, {OLD_ID, second_id})
        self.assertTrue(all(r["source_name"] == "aubing-market.pdf"
                            for r in routes))

        (job / "meta.json").unlink()
        fallback = route_catalog.list_routes(self.cache, self.jobs, HASH)
        self.assertTrue(any(r["title"] == "Aubing circuit" for r in fallback))

    def test_source_filename_becomes_place_and_event_date(self):
        self.make_route()
        job = self.jobs / "stadtpark"
        job.mkdir()
        (job / "meta.json").write_text(json.dumps({
            "pdf_hash": HASH,
            "source_name": "hofflohmaerkte-stadtparkviertel-181026-neu.pdf",
        }))

        route = route_catalog.list_routes(self.cache, self.jobs, HASH)[0]
        self.assertEqual(route["title"], "Stadtparkviertel")
        self.assertEqual(route["map_date"], "2026-10-18")
        self.assertTrue(route["updated_map"])

    def test_compact_direction_suffix_is_readable(self):
        self.make_route()
        job = self.jobs / "pasing"
        job.mkdir()
        (job / "meta.json").write_text(json.dumps({
            "pdf_hash": HASH,
            "source_name": "hofflohmaerkte-pasingnord-171026.pdf",
        }))

        route = route_catalog.list_routes(self.cache, self.jobs, HASH)[0]
        self.assertEqual(route["title"], "Pasing Nord")
        self.assertEqual(route["map_date"], "2026-10-17")
        self.assertFalse(route["updated_map"])

    def test_legacy_route_name_without_filename_uses_place_or_honest_fallback(self):
        self.make_route(name="Hofflohmaerkte loop from Essen-Kupferdreh")
        route = route_catalog.list_routes(self.cache, self.jobs, HASH)[0]
        self.assertEqual(route["title"], "Essen-Kupferdreh")

        summary_path = self.cache / OLD_ID / "raw_summary.json"
        summary = json.loads(summary_path.read_text())
        summary["variants"][0]["name"] = "Hofflohmaerkte circular tour (dots only)"
        summary_path.write_text(json.dumps(summary))
        route = route_catalog.list_routes(self.cache, self.jobs, HASH)[0]
        self.assertEqual(route["title"], "Unnamed flyer")

    def test_incomplete_entries_are_not_listed(self):
        good = self.make_route()
        broken = self.make_route("a" * 20 + "_" + "d" * 20)
        (broken / "route_circle.pdf").unlink()
        malformed = self.cache / ("a" * 20 + "_" + "e" * 20)
        malformed.mkdir()
        (malformed / "raw_summary.json").write_text("{")
        (self.cache / ".staging-abcd").mkdir()

        self.assertEqual([r["id"] for r in route_catalog.list_routes(
            self.cache, self.jobs)], [good.name])

    def test_route_without_original_flyer_preview_is_not_listed(self):
        route = self.make_route()
        summary_path = route / "raw_summary.json"
        summary = json.loads(summary_path.read_text())
        summary["files"].remove("original.png")
        summary_path.write_text(json.dumps(summary))
        self.assertEqual(route_catalog.list_routes(self.cache, self.jobs), [])

    def test_publish_supersedes_only_selected_entry_and_preserves_old_files(self):
        self.make_route()
        other_id = "a" * 20 + "_" + "c" * 20
        self.make_route(other_id)
        output = Path(self.tmp.name) / "output"
        output.mkdir()
        (output / "route_circle.pdf").write_bytes(b"%PDF-new")
        (output / "route_circle.png").write_bytes(b"new image")
        (output / "original.png").write_bytes(b"flyer")
        summary = json.loads((self.cache / OLD_ID / "raw_summary.json").read_text())
        metadata = {"pdf_hash": HASH, "source_name": "aubing-market.pdf",
                    "created_at": "2026-10-06T12:00:00Z", "start": None,
                    "end": None, "calibration_hash": "nocal"}

        new_id = route_catalog.publish_route(
            self.cache, OLD_ID, output, summary, metadata, supersedes=OLD_ID)

        self.assertEqual({r["id"] for r in route_catalog.list_routes(
            self.cache, self.jobs, HASH)}, {new_id, other_id})
        self.assertEqual((self.cache / new_id / "route_circle.pdf").read_bytes(),
                         b"%PDF-new")
        self.assertIsNotNone(route_catalog.get_route(self.cache, self.jobs, OLD_ID))
        self.assertEqual((self.cache / OLD_ID / "route_circle.pdf").read_bytes(),
                         b"%PDF-route")

    def test_failed_publication_leaves_old_route_visible(self):
        self.make_route()
        output = Path(self.tmp.name) / "empty-output"
        output.mkdir()
        summary = json.loads((self.cache / OLD_ID / "raw_summary.json").read_text())
        with self.assertRaises((FileNotFoundError, ValueError)):
            route_catalog.publish_route(
                self.cache, OLD_ID, output, summary,
                {"pdf_hash": HASH, "source_name": "aubing-market.pdf"},
                supersedes=OLD_ID)
        self.assertEqual([r["id"] for r in route_catalog.list_routes(
            self.cache, self.jobs, HASH)], [OLD_ID])

    def test_delete_map_removes_all_route_generations_only_for_that_map(self):
        first = self.make_route()
        second_id = "a" * 20 + "_" + "c" * 20
        self.make_route(second_id)
        other_id = "d" * 20 + "_" + "e" * 20
        self.make_route(other_id)
        summary = json.loads((first / "raw_summary.json").read_text())
        replacement = route_catalog.publish_route(
            self.cache, OLD_ID, first, summary,
            {"pdf_hash": HASH, "source_name": "aubing.pdf"},
            supersedes=OLD_ID)

        removed = route_catalog.delete_map(self.cache, HASH[:20])

        self.assertEqual(removed, 3)
        self.assertEqual([r["id"] for r in route_catalog.list_routes(
            self.cache, self.jobs)], [other_id])
        self.assertFalse((self.cache / OLD_ID).exists())
        self.assertFalse((self.cache / second_id).exists())
        self.assertFalse((self.cache / replacement).exists())

    def test_delete_map_rejects_invalid_prefix(self):
        self.make_route()
        with self.assertRaises(ValueError):
            route_catalog.delete_map(self.cache, "../")
        self.assertTrue((self.cache / OLD_ID).exists())

    def test_delete_all_removes_every_cached_map(self):
        self.make_route()
        other_id = "d" * 20 + "_" + "e" * 20
        self.make_route(other_id)

        removed = route_catalog.delete_all(self.cache)

        self.assertEqual(removed, 2)
        self.assertEqual(route_catalog.list_routes(self.cache, self.jobs), [])

    def test_delete_all_waits_for_active_map_calculation(self):
        self.make_route()
        entered = threading.Event()
        release = threading.Event()
        deleted = threading.Event()

        def solve():
            with route_catalog.map_lock(self.cache, HASH):
                entered.set()
                release.wait(2)

        def clear():
            entered.wait(2)
            route_catalog.delete_all(self.cache)
            deleted.set()

        first = threading.Thread(target=solve)
        second = threading.Thread(target=clear)
        first.start()
        second.start()
        self.assertTrue(entered.wait(2))
        time.sleep(0.05)
        self.assertFalse(deleted.is_set())
        release.set()
        first.join(2)
        second.join(2)
        self.assertTrue(deleted.is_set())
        self.assertEqual(route_catalog.list_routes(self.cache, self.jobs), [])

    def test_same_map_lock_serializes_threads(self):
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first():
            with route_catalog.map_lock(self.cache, HASH):
                entered.set()
                release.wait(2)

        def second():
            entered.wait(2)
            with route_catalog.map_lock(self.cache, HASH):
                second_entered.set()

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        self.assertTrue(entered.wait(2))
        time.sleep(0.05)
        self.assertFalse(second_entered.is_set())
        with route_catalog.map_lock(self.cache, "b" * 64):
            pass
        release.set()
        t1.join(2)
        t2.join(2)
        self.assertTrue(second_entered.is_set())

    def test_full_hash_and_prefix_share_a_map_lock(self):
        entered = threading.Event()
        release = threading.Event()
        deleted = threading.Event()

        def solve():
            with route_catalog.map_lock(self.cache, HASH):
                entered.set()
                release.wait(2)

        def delete():
            entered.wait(2)
            with route_catalog.map_lock(self.cache, HASH[:20]):
                deleted.set()

        first = threading.Thread(target=solve)
        second = threading.Thread(target=delete)
        first.start()
        second.start()
        self.assertTrue(entered.wait(2))
        time.sleep(0.05)
        self.assertFalse(deleted.is_set())
        release.set()
        first.join(2)
        second.join(2)
        self.assertTrue(deleted.is_set())


if __name__ == "__main__":
    unittest.main()
