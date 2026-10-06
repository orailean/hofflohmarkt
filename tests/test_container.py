from pathlib import Path
import json
import subprocess
import unittest


class ContainerRuntimeTests(unittest.TestCase):
    def test_runner_copies_all_local_python_runtime_modules(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        runner = dockerfile.split("FROM base AS runner", 1)[1]

        for module in (
            "webapp.py",
            "hoffroute.py",
            "flyer_streets.py",
            "station_resolver.py",
            "route_catalog.py",
        ):
            self.assertIn(module, runner)

    def test_route_cache_survives_container_replacement(self):
        configured = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            check=True, capture_output=True, text=True)
        compose = json.loads(configured.stdout)
        mounts = compose["services"]["hoffroute"]["volumes"]
        self.assertTrue(any(
            volume["type"] == "volume" and
            volume["source"] == "route_cache" and
            volume["target"] == "/data/route_cache"
            for volume in mounts))
        self.assertIn("route_cache", compose["volumes"])


if __name__ == "__main__":
    unittest.main()
