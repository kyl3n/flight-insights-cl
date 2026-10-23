#!/usr/bin/env python3
"""Fail if git tracks flight data or secrets where they must not be.

Flight logs, tlogs and parameter files reveal positions and aircraft
identifiers. Scrubbed fixtures are allowed only under tests/fixtures/, and
each one must be listed with its SHA-256 in tests/fixtures/SCRUBBED.sha256,
which tools/fixtures/scrub_fixture.py writes. Key material is never allowed
anywhere. See CLAUDE.md, "Location and privacy".

A file is flight data if its name has a flight-data suffix, or if its content
starts like a DataFlash log or a tlog, whatever it is called.

Usage:
  check_repo_files.py [path ...]          every file git tracks, or the paths given
  check_repo_files.py --commits BASE..HEAD
      every file added or modified by each commit in the range, as it was in
      that commit, so flight data added and later deleted is caught too
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

FIXTURES_DIR = PurePosixPath("tests/fixtures")
MANIFEST = FIXTURES_DIR / "SCRUBBED.sha256"

# Flight data: allowed only under tests/fixtures/.
FLIGHT_DATA_SUFFIXES = {
    ".bin",
    ".tlog",
    ".rlog",
    ".log",
    ".param",
    ".parm",
    ".params",
    ".waypoints",
    ".plan",
}

# Secrets: never allowed.
SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SECRET_NAME_PARTS = ("recovery-key",)

# A DataFlash log starts with an FMT message: header 0xA3 0x95, then id 0x80.
DATAFLASH_MAGIC = b"\xa3\x95\x80"
# A tlog is records of an 8-byte big-endian time and one MAVLink packet.
TLOG_MIN_RECORDS = 3
# Enough of a file's start to see TLOG_MIN_RECORDS of the largest MAVLink packets.
HEAD_SIZE = 1024
GITLINK_MODE = "160000"


def _suffixes(path: PurePosixPath) -> set[str]:
    # "flight.tlog.raw" counts as ".tlog"; compare case-insensitively.
    return {s.lower() for s in path.suffixes}


def _is_env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def looks_like_tlog(head: bytes) -> bool:
    """True if `head` starts with TLOG_MIN_RECORDS tlog records, or is a whole shorter tlog."""
    o = records = 0
    while records < TLOG_MIN_RECORDS and o + 10 <= len(head):
        magic, length = head[o + 8], head[o + 9]
        if magic == 0xFE:  # MAVLink 1: 6-byte header, payload, 2-byte CRC
            size = 8 + length
        elif magic == 0xFD:  # MAVLink 2: 10-byte header, payload, CRC, optional signature
            size = 12 + length + (13 if o + 10 < len(head) and head[o + 10] & 0x01 else 0)
        else:
            return False
        o += 8 + size
        if o > len(head):
            return False
        records += 1
    return records >= TLOG_MIN_RECORDS or (records > 0 and o == len(head))


def flight_data_content(head: bytes) -> str | None:
    if head.startswith(DATAFLASH_MAGIC):
        return "DataFlash log"
    if looks_like_tlog(head):
        return "tlog"
    return None


def is_flight_data(path: PurePosixPath, head: bytes) -> bool:
    return bool(_suffixes(path) & FLIGHT_DATA_SUFFIXES) or flight_data_content(head) is not None


def problems_for(path: PurePosixPath, head: bytes) -> list[str]:
    """Return the reasons a tracked file is not allowed, or an empty list."""
    found: list[str] = []
    name = path.name.lower()

    if (
        _suffixes(path) & SECRET_SUFFIXES
        or _is_env_file(name)
        or any(part in name for part in SECRET_NAME_PARTS)
    ):
        found.append("looks like a secret or key file")

    if not path.is_relative_to(FIXTURES_DIR):
        if _suffixes(path) & FLIGHT_DATA_SUFFIXES:
            found.append("flight data outside tests/fixtures/")
        elif (content := flight_data_content(head)) is not None:
            found.append(f"{content} content outside tests/fixtures/")

    return found


def read_manifest(text: str) -> dict[str, str]:
    """`sha256sum` format: digest, two spaces, path relative to tests/fixtures/."""
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip():
            digest, rel = line.split(None, 1)
            entries[rel.strip()] = digest
    return entries


def fixture_problems(path: PurePosixPath, content: bytes, manifest: dict[str, str]) -> list[str]:
    """A fixture must be listed in the manifest with the hash the scrubber recorded."""
    if not path.is_relative_to(FIXTURES_DIR) or not is_flight_data(path, content[:HEAD_SIZE]):
        return []
    rel = path.relative_to(FIXTURES_DIR).as_posix()
    if rel not in manifest:
        return ["fixture not in SCRUBBED.sha256: was it scrubbed?"]
    if hashlib.sha256(content).hexdigest() != manifest[rel]:
        return ["fixture changed since it was scrubbed"]
    return []


def tracked_files(repo: Path) -> list[PurePosixPath]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    ).stdout
    return [PurePosixPath(p) for p in out.decode().split("\0") if p]


def check(repo: Path, paths: Iterable[PurePosixPath]) -> list[str]:
    errors: list[str] = []
    manifest_file = repo / MANIFEST
    manifest = read_manifest(manifest_file.read_text()) if manifest_file.is_file() else {}
    for rel in paths:
        full = repo / rel
        content = full.read_bytes() if full.is_file() else b""
        reasons = problems_for(rel, content[:HEAD_SIZE])
        reasons += fixture_problems(rel, content, manifest)
        errors.extend(f"{rel}: {reason}" for reason in reasons)
    return errors


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def changed_blobs(repo: Path, commit: str) -> list[tuple[PurePosixPath, str]]:
    """(path, blob id) for every file the commit adds, modifies or retypes, against each parent."""
    out = _git(
        repo, "diff-tree", "--no-commit-id", "-r", "-m", "--root", "-z", "--diff-filter=AMT", commit
    )
    fields = out.split(b"\0")
    found: dict[tuple[PurePosixPath, str], None] = {}
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        # ":<old mode> <new mode> <old blob> <new blob> <status>", then the path
        _, new_mode, _, new_blob, _ = meta.decode().lstrip(":").split(" ")
        if new_mode != GITLINK_MODE:
            found[(PurePosixPath(path.decode()), new_blob)] = None
    return list(found)


def check_commits(repo: Path, revision_range: str) -> tuple[int, list[str]]:
    """Check every path added or modified in each commit of the range, as of that commit."""
    commits = _git(repo, "rev-list", "--reverse", revision_range).decode().split()
    errors: list[str] = []
    checked = 0
    for commit in commits:
        try:
            manifest = read_manifest(_git(repo, "show", f"{commit}:{MANIFEST}").decode())
        except subprocess.CalledProcessError:
            manifest = {}
        for rel, blob in changed_blobs(repo, commit):
            content = _git(repo, "cat-file", "blob", blob)
            reasons = problems_for(rel, content[:HEAD_SIZE])
            reasons += fixture_problems(rel, content, manifest)
            errors.extend(f"{commit[:12]} {rel}: {reason}" for reason in reasons)
            checked += 1
    return checked, errors


def main(argv: Sequence[str]) -> int:
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    if argv[:1] == ["--commits"]:
        if len(argv) != 2:
            print("usage: check_repo_files.py --commits BASE..HEAD", file=sys.stderr)
            return 2
        count, errors = check_commits(repo, argv[1])
        what = f"file version(s) in {argv[1]}"
    else:
        if argv:
            paths = [PurePosixPath(Path(p).resolve().relative_to(repo).as_posix()) for p in argv]
        else:
            paths = tracked_files(repo)
        count, errors = len(paths), check(repo, paths)
        what = "file(s)"
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print(
            f"\n{len(errors)} file(s) not allowed. Real flight data and secrets must never be "
            "committed; scrubbed fixtures belong in tests/fixtures/.",
            file=sys.stderr,
        )
        return 1
    print(f"Checked {count} {what}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
