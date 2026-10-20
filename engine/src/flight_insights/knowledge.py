"""The knowledge pack: versioned data files the engine reads (spec: System architecture).

The pack lives outside the engine so it can be updated without an app release.
The shell tells the engine where it is with FI_KNOWLEDGE_DIR; in a source
checkout the repository's knowledge/ folder is found on its own.
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

Vehicle = str  # "copter" (helicopters included) or "plane" (QuadPlanes included)


class KnowledgeError(RuntimeError):
    """The knowledge pack is missing or does not hold what was asked for."""


def knowledge_dir() -> Path:
    env = os.environ.get("FI_KNOWLEDGE_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "knowledge" / "manifest.json").is_file():
            return parent / "knowledge"
    raise KnowledgeError("knowledge pack not found: set FI_KNOWLEDGE_DIR")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit())


@dataclass(frozen=True)
class VersionChoice:
    version: str  # the release whose metadata is used
    exact: bool  # False: the firmware's own release is not in the pack


@dataclass(frozen=True)
class Pack:
    root: Path
    manifest: dict[str, Any]

    @property
    def versions(self) -> list[str]:
        return sorted(self.manifest.get("params", {}), key=version_key)

    def choose(self, firmware: str) -> VersionChoice:
        """The pack release that describes `firmware`: itself, or the nearest one before it.

        Firmware older than the pack's oldest release (older than the fleet's oldest,
        4.7.0) is read with the oldest release, marked inexact.
        """
        versions = self.versions
        if not versions:
            raise KnowledgeError("the knowledge pack has no parameter metadata")
        if firmware in versions:
            return VersionChoice(firmware, True)
        earlier = [v for v in versions if version_key(v) <= version_key(firmware)]
        return VersionChoice(earlier[-1] if earlier else versions[0], False)

    def metadata(self, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
        """Every parameter of a release: ArduPilot's generated metadata plus the curated
        supplement (params/supplement.json) for parameters the generator leaves out."""
        return _metadata(self.root, version, vehicle)

    def generated_metadata(self, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
        return _generated(self.root, version, vehicle)

    def json(self, rel: str) -> Any:
        return json.loads((self.root / rel).read_text())


@cache
def _metadata(root: Path, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
    merged = dict(_generated(root, version, vehicle))
    supplement = root / "params" / "supplement.json"
    if supplement.is_file():
        for p in json.loads(supplement.read_text())["params"]:
            if vehicle in p["vehicles"] and version_key(version) >= version_key(p["from_version"]):
                merged.setdefault(p["name"], p["vehicles"][vehicle] | {"supplement": True})
    return merged


@cache
def _generated(root: Path, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text())
    try:
        rel = manifest["params"][version]["files"][vehicle]
    except KeyError as e:
        raise KnowledgeError(f"no {vehicle} parameter metadata for {version}") from e
    with gzip.open(root / rel) as f:
        return json.load(f)


def load_pack(root: Path | None = None) -> Pack:
    root = root or knowledge_dir()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise KnowledgeError(f"no manifest.json in {root}")
    return Pack(root, json.loads(manifest_path.read_text()))
