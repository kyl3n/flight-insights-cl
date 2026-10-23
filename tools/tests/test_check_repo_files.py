import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_repo_files import (
    DATAFLASH_MAGIC,
    check_commits,
    fixture_problems,
    looks_like_tlog,
    problems_for,
    read_manifest,
)


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


def tlog_record(i: int, v2: bool = False, payload: bytes = b"\x01" * 9) -> bytes:
    stamp = struct.pack(">Q", 1_700_000_000_000_000 + i * 1000)
    if v2:
        packet = bytes([0xFD, len(payload), 0, 0, i, 1, 1, 0, 0, 0]) + payload + b"\0\0"
    else:
        packet = bytes([0xFE, len(payload), i, 1, 1, 0]) + payload + b"\0\0"
    return stamp + packet


TLOG = b"".join(tlog_record(i, v2=i % 2 == 1) for i in range(5))


class TlogContentTest(unittest.TestCase):
    def test_a_renamed_tlog_is_caught_by_its_content(self) -> None:
        for path in ["docs/capture.dat", "notes.txt", "data/blob"]:
            with self.subTest(path=path):
                self.assertEqual(problems(path, TLOG), ["tlog content outside tests/fixtures/"])

    def test_a_short_whole_tlog_counts(self) -> None:
        self.assertTrue(looks_like_tlog(tlog_record(0)))

    def test_one_record_then_something_else_is_not_a_tlog(self) -> None:
        self.assertFalse(looks_like_tlog(tlog_record(0) + b"plain text after it, not a record"))

    def test_text_and_other_binaries_are_not_tlogs(self) -> None:
        for head in [b"", b"# heading\n" * 20, bytes(range(256)), b"\x89PNG\r\n\x1a\n" + bytes(64)]:
            with self.subTest(head=head[:12]):
                self.assertFalse(looks_like_tlog(head))

    def test_tlog_fixture_with_another_name_needs_a_manifest_entry(self) -> None:
        self.assertEqual(
            fixture_problems(PurePosixPath("tests/fixtures/capture.dat"), TLOG, {}),
            ["fixture not in SCRUBBED.sha256: was it scrubbed?"],
        )


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


class ManifestTest(unittest.TestCase):
    LOG = DATAFLASH_MAGIC + b"rest of a scrubbed log"
    DIGEST = hashlib.sha256(LOG).hexdigest()

    def check(self, path: str, content: bytes, manifest: str) -> list[str]:
        return fixture_problems(PurePosixPath(path), content, read_manifest(manifest))

    def test_listed_fixture_with_matching_hash_passes(self) -> None:
        manifest = f"{self.DIGEST}  sitl/4.7.0/hover.bin\n"
        self.assertEqual(self.check("tests/fixtures/sitl/4.7.0/hover.bin", self.LOG, manifest), [])

    def test_unlisted_fixture_fails(self) -> None:
        self.assertEqual(
            self.check("tests/fixtures/new.tlog", b"x", ""),
            ["fixture not in SCRUBBED.sha256: was it scrubbed?"],
        )

    def test_fixture_edited_after_scrubbing_fails(self) -> None:
        manifest = f"{self.DIGEST}  hover.bin\n"
        self.assertEqual(
            self.check("tests/fixtures/hover.bin", self.LOG + b"!", manifest),
            ["fixture changed since it was scrubbed"],
        )

    def test_renamed_log_is_still_checked(self) -> None:
        self.assertEqual(
            self.check("tests/fixtures/notes.dat", self.LOG, ""),
            ["fixture not in SCRUBBED.sha256: was it scrubbed?"],
        )

    def test_labels_and_docs_need_no_entry(self) -> None:
        for path in ["tests/fixtures/README.md", "tests/fixtures/sitl/4.7.0/hover.yaml"]:
            with self.subTest(path=path):
                self.assertEqual(self.check(path, b"text", ""), [])


class CommitHistoryTest(unittest.TestCase):
    """--commits checks each commit's files, so flight data deleted later still fails."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.commit({"README.md": b"# test\n"}, "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        env = os.environ | {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=env, check=True, capture_output=True, text=True
        ).stdout

    def commit(self, files: dict[str, bytes | None], message: str) -> None:
        for rel, content in files.items():
            path = self.repo / rel
            if content is None:
                self.git("rm", "-q", rel)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.git("add", rel)
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def errors(self) -> list[str]:
        return [e.split(" ", 1)[1] for e in check_commits(self.repo, f"{self.base}..HEAD")[1]]

    def test_flight_data_added_then_deleted_fails(self) -> None:
        self.commit({"logs/flight.bin": DATAFLASH_MAGIC + b"log"}, "add a log")
        self.commit({"logs/flight.bin": None}, "remove it again")
        self.assertEqual(self.errors(), ["logs/flight.bin: flight data outside tests/fixtures/"])

    def test_content_is_read_from_the_commit(self) -> None:
        self.commit({"docs/capture.dat": TLOG}, "a renamed tlog")
        self.commit({"docs/capture.dat": b"replaced with text\n"}, "hide it")
        self.assertEqual(self.errors(), ["docs/capture.dat: tlog content outside tests/fixtures/"])

    def test_flight_data_brought_in_by_a_merge_fails(self) -> None:
        self.git("checkout", "-q", "-b", "side")
        self.commit({"side.tlog": TLOG}, "side work")
        self.commit({"side.tlog": None}, "tidy up")
        self.git("checkout", "-q", "main")
        self.commit({"other.md": b"x\n"}, "main work")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side")
        self.assertIn("side.tlog: flight data outside tests/fixtures/", self.errors())

    def test_fixtures_are_checked_against_the_manifest_of_their_commit(self) -> None:
        log = DATAFLASH_MAGIC + b"scrubbed"
        manifest = f"{hashlib.sha256(log).hexdigest()}  hover.bin\n".encode()
        self.commit(
            {"tests/fixtures/hover.bin": log, "tests/fixtures/SCRUBBED.sha256": manifest}, "ok"
        )
        self.assertEqual(self.errors(), [])
        self.commit({"tests/fixtures/hover.bin": log + b"real data"}, "unscrubbed edit")
        self.commit({"tests/fixtures/hover.bin": log}, "put it back")
        self.assertEqual(
            self.errors(), ["tests/fixtures/hover.bin: fixture changed since it was scrubbed"]
        )

    def test_clean_history_passes(self) -> None:
        self.commit({"engine/x.py": b"print(1)\n"}, "code")
        self.commit({"engine/x.py": b"print(2)\n"}, "more code")
        self.assertEqual(self.errors(), [])


if __name__ == "__main__":
    unittest.main()
