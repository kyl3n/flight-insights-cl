import sys
import unittest
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_repo_files import DATAFLASH_MAGIC, problems_for  # noqa: E402


def problems(path: str, head: bytes = b"") -> list[str]:
    return problems_for(PurePosixPath(path), head)


class FlightDataTest(unittest.TestCase):
    def test_flight_data_outside_fixtures_is_rejected(self) -> None:
        for path in [
            "00000042.BIN",
            "logs/flight.bin",
            "2026-10-20 flight.tlog",
            "flight.tlog.raw",
            "engine/airframe.param",
            "qgc/airframe.params",
            "mission.waypoints",
            "mission.plan",
        ]:
            with self.subTest(path=path):
                self.assertEqual(problems(path), ["flight data outside tests/fixtures/"])

    def test_fixtures_are_allowed(self) -> None:
        for path in [
            "tests/fixtures/copter-4.7.0-sitl.bin",
            "tests/fixtures/tlog/hover.tlog",
            "tests/fixtures/params/copter.param",
        ]:
            with self.subTest(path=path):
                self.assertEqual(problems(path, DATAFLASH_MAGIC), [])

    def test_dataflash_content_is_caught_whatever_the_name(self) -> None:
        self.assertEqual(
            problems("docs/renamed.dat", DATAFLASH_MAGIC + b"\x59"),
            ["DataFlash log content outside tests/fixtures/"],
        )

    def test_a_directory_named_like_fixtures_elsewhere_is_not_exempt(self) -> None:
        self.assertEqual(
            problems("engine/tests/fixtures/flight.bin"),
            ["flight data outside tests/fixtures/"],
        )

    def test_ordinary_files_pass(self) -> None:
        for path in ["README.md", "engine/src/flight_insights/cli.py", "ui/src/App.svelte"]:
            with self.subTest(path=path):
                self.assertEqual(problems(path, b"# h"), [])


class SecretsTest(unittest.TestCase):
    def test_secrets_are_rejected_everywhere(self) -> None:
        for path in [
            ".env",
            "shell/.env.local",
            "signing.key",
            "certs/codesign.pfx",
            "tests/fixtures/server.pem",
            "admin-recovery-key.txt",
        ]:
            with self.subTest(path=path):
                self.assertIn("looks like a secret or key file", problems(path))


if __name__ == "__main__":
    unittest.main()
