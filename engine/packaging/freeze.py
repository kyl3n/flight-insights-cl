"""Freeze the engine into one executable with PyInstaller, named as Tauri expects a
sidecar: fi-engine-<target triple>[.exe] (https://v2.tauri.app/develop/sidecar).

The knowledge pack is not frozen in: it ships beside the app and is updated without
a release, and the shell passes its location in FI_KNOWLEDGE_DIR.

Usage (from engine/): uv run python packaging/freeze.py [--dist DIR]
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DEFAULT_DIST = ENGINE.parent / "shell" / "src-tauri" / "binaries"
TRIPLES = {
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
}
# Imported by name at run time, so PyInstaller's import analysis cannot see them.
HIDDEN = [
    "pymavlink.dialects.v10.ardupilotmega",
    "pymavlink.dialects.v20.ardupilotmega",
    "pymavlink.generator.mavparse",
]
EXCLUDE = ["tkinter", "matplotlib", "IPython", "pytest", "hypothesis"]


def target_triple() -> str:
    try:
        out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
        host = (line for line in out.splitlines() if line.startswith("host: "))
        return next(host).split(": ", 1)[1]
    except (OSError, subprocess.CalledProcessError, StopIteration):
        key = (platform.system(), platform.machine())
        if key not in TRIPLES:
            raise SystemExit(f"no target triple known for {key}; install rustc") from None
        return TRIPLES[key]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST)
    args = parser.parse_args()
    import PyInstaller.__main__  # dev dependency

    name = f"fi-engine-{target_triple()}"
    work = ENGINE / "build" / "pyinstaller"
    argv = [
        str(ENGINE / "src" / "flight_insights" / "__main__.py"),
        "--onefile",
        "--noconfirm",
        "--clean",
        "--name", name,
        "--distpath", str(args.dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--collect-data", "flight_insights",  # migrations, py.typed
        "--additional-hooks-dir", str(ENGINE / "packaging" / "hooks"),  # a leaner pyarrow
        *[a for h in HIDDEN for a in ("--hidden-import", h)],
        *[a for m in EXCLUDE for a in ("--exclude-module", m)],
    ]  # fmt: skip
    PyInstaller.__main__.run(argv)
    suffix = ".exe" if sys.platform == "win32" else ""
    print(args.dist / f"{name}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
