#!/usr/bin/env python3
"""Generate the knowledge pack's parameter metadata for one ArduPilot release.

Runs ArduPilot's own generator (Tools/autotest/param_metadata/param_parse.py, the
one that produces apm.pdef.json for ground stations) on a checkout of the
release, for Copter (which includes helicopters) and Plane (which includes
QuadPlanes). The result is normalised, gzipped reproducibly, and recorded in
knowledge/manifest.json with the ArduPilot commit it came from.

Usage (from the repository root):
  python3 knowledge/tools/build_param_metadata.py ARDUPILOT_DIR VERSION

Run it with a Python that has ArduPilot's build requirements (see
.github/workflows/fixtures.yml); param_parse.py needs no others.
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PACK = Path(__file__).resolve().parents[1]
VEHICLES = {"copter": "ArduCopter", "plane": "ArduPlane"}


def _float(text: Any) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def normalise(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """apm.pdef.json (groups of parameters) -> {name: metadata}, sorted, compact."""
    out: dict[str, dict[str, Any]] = {}
    for group in raw.values():
        for name, p in group.items():
            if not isinstance(p, dict):
                continue  # the "json": {"version": n} entry
            entry: dict[str, Any] = {
                "display": p.get("DisplayName", ""),
                "description": p.get("Description", ""),
            }
            if p.get("Units"):
                entry["units"] = p["Units"]
            rng = p.get("Range")
            if isinstance(rng, dict):
                entry["range"] = [_float(rng.get("low")), _float(rng.get("high"))]
            if p.get("Values"):
                entry["values"] = p["Values"]
            if p.get("Bitmask"):
                entry["bitmask"] = p["Bitmask"]
            if _float(p.get("Increment")) is not None:
                entry["increment"] = _float(p.get("Increment"))
            if p.get("User"):
                entry["user"] = p["User"]
            for key, flag in (
                ("RebootRequired", "reboot_required"),
                ("ReadOnly", "read_only"),
                ("Volatile", "volatile"),
                ("Calibration", "calibration"),
            ):
                if str(p.get(key, "")).lower() in ("true", "1"):
                    entry[flag] = True
            out[name] = entry
    return dict(sorted(out.items()))


def write_gzip_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=0, sort_keys=True, ensure_ascii=False).encode()
    with path.open("wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as gz:
        gz.write(body)


def generate(ardupilot: Path, vehicle: str) -> dict[str, Any]:
    script = ardupilot / "Tools" / "autotest" / "param_metadata" / "param_parse.py"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [sys.executable, str(script), "--vehicle", vehicle, "--format", "json"],
            cwd=tmp,
            check=True,
            capture_output=True,
        )
        return json.loads((Path(tmp) / "apm.pdef.json").read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ardupilot", type=Path)
    parser.add_argument("version", help="release, e.g. 4.7.1")
    args = parser.parse_args()
    commit = subprocess.run(
        ["git", "-C", str(args.ardupilot), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest_path = PACK / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    files: dict[str, str] = {}
    counts: dict[str, int] = {}
    for short, vehicle in VEHICLES.items():
        params = normalise(generate(args.ardupilot, vehicle))
        rel = f"params/{args.version}/{short}.json.gz"
        write_gzip_json(PACK / rel, params)
        files[short], counts[short] = rel, len(params)
        print(f"{args.version} {short}: {len(params)} parameters")
    manifest.setdefault("params", {})[args.version] = {
        "ardupilot_commit": commit,
        "generator": "Tools/autotest/param_metadata/param_parse.py",
        "files": files,
        "counts": counts,
    }
    manifest["params"] = dict(
        sorted(manifest["params"].items(), key=lambda kv: [int(x) for x in kv[0].split(".")])
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
