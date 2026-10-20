#!/usr/bin/env python3
"""Fail if git tracks flight data or secrets where they must not be.

Flight logs, tlogs and parameter files reveal positions and aircraft
identifiers. Scrubbed fixtures are allowed only under tests/fixtures/, and
each one must be listed with its SHA-256 in tests/fixtures/SCRUBBED.sha256,
which tools/fixtures/scrub_fixture.py writes. Key material is never allowed
anywhere. See CLAUDE.md, "Location and privacy".

Usage: check_repo_files.py [path ...]   (default: every file git tracks)
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


def _suffixes(path: PurePosixPath) -> set[str]:
    # "flight.tlog.raw" counts as ".tlog"; compare case-insensitively.
    return {s.lower() for s in path.suffixes}


def _is_env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def is_flight_data(path: PurePosixPath, head: bytes) -> bool:
    return bool(_suffixes(path) & FLIGHT_DATA_SUFFIXES) or head.startswith(DATAFLASH_MAGIC)


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
        elif head.startswith(DATAFLASH_MAGIC):
            found.append("DataFlash log content outside tests/fixtures/")

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
    if not path.is_relative_to(FIXTURES_DIR) or not is_flight_data(
        path, content[: len(DATAFLASH_MAGIC)]
    ):
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
        reasons = problems_for(rel, content[: len(DATAFLASH_MAGIC)])
        reasons += fixture_problems(rel, content, manifest)
        errors.extend(f"{rel}: {reason}" for reason in reasons)
    return errors


def main(argv: Sequence[str]) -> int:
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    if argv:
        paths = [PurePosixPath(Path(p).resolve().relative_to(repo).as_posix()) for p in argv]
    else:
        paths = tracked_files(repo)
    errors = check(repo, paths)
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        print(
            f"\n{len(errors)} file(s) not allowed. Real flight data and secrets must never be "
            "committed; scrubbed fixtures belong in tests/fixtures/.",
            file=sys.stderr,
        )
        return 1
    print(f"Checked {len(paths)} file(s): OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
