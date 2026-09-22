from pathlib import Path
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
        ):
            self.assertIn(module, runner)


if __name__ == "__main__":
    unittest.main()
