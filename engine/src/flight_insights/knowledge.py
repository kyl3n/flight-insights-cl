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
        return _nearest(versions, firmware)

    def enums(self, firmware: str) -> dict[str, Any]:
        """Mode and log-event names of a release (or the nearest earlier one in the pack):
        {"modes": {vehicle: {number: name}}, "events": {id: name}}."""
        entries: dict[str, Any] = self.manifest.get("enums", {})
        if not entries:
            raise KnowledgeError("the knowledge pack has no mode or event names")
        choice = _nearest(sorted(entries, key=version_key), firmware)
        return self.json(entries[choice.version]["file"])

    def boards(self, firmware: str) -> dict[str, dict[str, Any]]:
        """Board short name -> {hwdef, mcu, family, apj_board_id} for a release (or the
        nearest earlier one in the pack)."""
        entries: dict[str, Any] = self.manifest.get("boards", {})
        if not entries:
            raise KnowledgeError("the knowledge pack has no board table")
        choice = _nearest(sorted(entries, key=version_key), firmware)
        return self.json(entries[choice.version]["file"])["boards"]

    def frames(self, firmware: str) -> dict[str, dict[str, Any]]:
        """FRAME_CLASS -> {name, types: FRAME_TYPE (or "default") -> {name, motors, and
        vehicles when only some build it}}: each motor's roll, pitch, yaw and throttle
        factors as the release's matrix mixer sets them (or the nearest earlier release's),
        read from its source by knowledge/tools/build_motor_frames.py."""
        entries: dict[str, Any] = self.manifest.get("frames", {})
        if not entries:
            raise KnowledgeError("the knowledge pack has no motor frame table")
        choice = _nearest(sorted(entries, key=version_key), firmware)
        return self.json(entries[choice.version]["file"])["classes"]

    def metadata(self, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
        """Every parameter of a release: ArduPilot's generated metadata plus the curated
        supplement (params/supplement.json) for parameters the generator leaves out."""
        return _metadata(self.root, version, vehicle)

    def generated_metadata(self, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
        return _generated(self.root, version, vehicle)

    def gates(self, version: str, vehicle: Vehicle) -> dict[str, dict[str, str]]:
        """Parameter -> {"gate", "after"} for a release, read from its source by
        knowledge/tools/build_param_gates.py: the parameter that switches it into view,
        and whether it then appears on a parameter refresh or only after a reboot. Empty
        when the pack has no table for the release."""
        return _gates(self.root, version, vehicle)

    def log_messages(self, version: str) -> dict[str, dict[str, Any]]:
        """Every log message a release can write: name -> {vehicles, fields: {name:
        {units, type}}}, from the release's own source (knowledge/logs)."""
        entries: dict[str, Any] = self.manifest.get("logs", {})
        if version not in entries:
            raise KnowledgeError(f"no log message definitions for {version}")
        return _log_messages(self.root, entries[version]["file"])

    def json(self, rel: str) -> Any:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))


def _nearest(versions: list[str], firmware: str) -> VersionChoice:
    if firmware in versions:
        return VersionChoice(firmware, True)
    earlier = [v for v in versions if version_key(v) <= version_key(firmware)]
    return VersionChoice(earlier[-1] if earlier else versions[0], False)


@cache
def _metadata(root: Path, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
    merged = dict(_generated(root, version, vehicle))
    supplement = root / "params" / "supplement.json"
    if supplement.is_file():
        for p in json.loads(supplement.read_text(encoding="utf-8"))["params"]:
            if vehicle in p["vehicles"] and version_key(version) >= version_key(p["from_version"]):
                merged.setdefault(p["name"], p["vehicles"][vehicle] | {"supplement": True})
    return merged


@cache
def _gates(root: Path, version: str, vehicle: Vehicle) -> dict[str, dict[str, str]]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest.get("gates", {}).get(version)
    if entry is None:
        return {}
    with gzip.open(root / entry["file"]) as f:
        return json.load(f).get(vehicle, {})


@cache
def _log_messages(root: Path, rel: str) -> dict[str, dict[str, Any]]:
    with gzip.open(root / rel) as f:
        return json.load(f)["messages"]


@cache
def _generated(root: Path, version: str, vehicle: Vehicle) -> dict[str, dict[str, Any]]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
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
    return Pack(root, json.loads(manifest_path.read_text(encoding="utf-8")))
