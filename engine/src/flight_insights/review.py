"""Check a parameter file (spec: Configuration review; Data sources, "audit without a
flight"; Platform setup, step 10 "First parameter file").

A `.param` or `.params` file is reviewed as a flight with no log behind it: its
parameters, read in the firmware's own dialect, and the setup answers of the airframe it
is for. Only the analyzers that judge parameters run (the parameter audit and Better
options); what only a log can show stays unknown, so it is asked about, never assumed.
The result is the same findings and staged plan a flight gets. Nothing is stored and
nothing is sent to the aircraft.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from flight_insights.analyzers.flight import FlightData, build_answers
from flight_insights.analyzers.model import Analyzer, Evidence
from flight_insights.analyzers.options import OPTIONS
from flight_insights.analyzers.params_audit import PARAMS_AUDIT
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import check_result
from flight_insights.fingerprint.attributes import parameter_record
from flight_insights.history.persist import load_snapshot
from flight_insights.history.snapshot import from_param_file
from flight_insights.knowledge import Pack
from flight_insights.params.files import read_param_file
from flight_insights.platforms.detect import vehicle_of_params
from flight_insights.recommend.plan import build_plan, to_json
from flight_insights.store.db import Store

FILE_ANALYZERS: tuple[Analyzer, ...] = (OPTIONS, PARAMS_AUDIT)


class ReviewError(ValueError):
    """The file cannot be reviewed: unreadable, empty, or for another vehicle."""


def _firmware(store: Store, airframe_id: str | None, pack: Pack) -> tuple[str, str]:
    """The firmware to read the file in: the airframe's latest flight's, else the
    newest the knowledge pack knows, which the result says it assumed."""
    if airframe_id is not None:
        row = store.conn.execute(
            "SELECT firmware FROM flight WHERE airframe_id = ? AND firmware IS NOT NULL "
            "ORDER BY created_hlc DESC LIMIT 1",
            (airframe_id,),
        ).fetchone()
        if row is not None and row[0] in pack.versions:
            return str(row[0]), "airframe"
    return pack.versions[-1], "newest"


def _defaults(
    store: Store, pack: Pack, airframe_id: str | None, vehicle: str, firmware: str
) -> tuple[dict[str, float], str | None]:
    """The firmware's defaults, which a parameter file does not record: borrowed from
    the log of a flight on the same firmware and vehicle, the airframe's own first.
    Without them, values left at default cannot be told from values chosen."""
    rows = store.conn.execute(
        "SELECT f.param_snapshot_id, f.airframe_id FROM flight f "
        "JOIN airframe a ON a.id = f.airframe_id JOIN platform p ON p.id = a.platform_id "
        "WHERE f.firmware = ? AND p.vehicle = ? AND f.param_snapshot_id IS NOT NULL "
        "ORDER BY f.created_hlc DESC",
        (firmware, vehicle),
    ).fetchall()
    rows.sort(key=lambda r: r[1] != airframe_id)  # the airframe's own flights first
    for snapshot_id, owner in rows:
        snapshot = load_snapshot(store, snapshot_id)
        if snapshot.defaults:
            source = "airframe" if owner == airframe_id else "fleet"
            return snapshot.dialect(pack).to_canonical(snapshot.defaults).values, source
    return {}, None


def review_file(
    store: Store, pack: Pack, path: Path, airframe_id: str | None = None
) -> dict[str, Any]:
    """The configuration review of one parameter file, for an airframe or none."""
    try:
        params_file = read_param_file(path)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ReviewError(f"cannot read {path.name}: {e}") from None
    names = {e.name for e in params_file.entries}
    answers: dict[str, Any] = {}
    vehicle: str | None = None
    if airframe_id is not None:
        row = store.conn.execute(
            "SELECT a.current_build_id, p.vehicle FROM airframe a "
            "JOIN platform p ON p.id = a.platform_id WHERE a.id = ?",
            (airframe_id,),
        ).fetchone()
        if row is None:
            raise ReviewError(f"no airframe {airframe_id}")
        answers, vehicle = build_answers(store, row[0]), str(row[1])
    try:
        found = vehicle_of_params(names)
    except ValueError as e:
        raise ReviewError(str(e)) from None
    if vehicle is not None and found != vehicle:
        raise ReviewError(f"the file is for a {found}, but the airframe is a {vehicle}")
    firmware, assumed = _firmware(store, airframe_id, pack)
    try:
        snapshot = from_param_file(params_file, found, firmware)
    except ValueError as e:
        raise ReviewError(str(e)) from None
    dialect = snapshot.dialect(pack)
    defaults, defaults_from = _defaults(store, pack, airframe_id, found, firmware)
    flight = FlightData(
        id="file",
        key="file",
        airframe_id=airframe_id,
        firmware=firmware,
        arm_us=0,
        disarm_us=0,
        params=dialect.to_canonical(snapshot.values).values,
        fingerprint=parameter_record(snapshot, pack),
        cache=None,
        answers=answers,
        defaults=defaults,
    )
    reg = Registry.load(pack)
    findings: list[dict[str, Any]] = []
    evidence: dict[str, Evidence] = {}
    skipped: dict[str, str | None] = {}
    for a in FILE_ANALYZERS:
        r = a.run(flight, reg)
        check_result(a, r, reg)
        skipped[a.name] = r.skipped
        for f in r.findings:
            for ev in f.evidence:
                evidence[ev.id] = ev
            findings.append(
                {"id": f"file:{len(findings) + 1}", "analyzer": a.name, "code": f.code,
                 "severity": f.severity, "confidence": f.confidence, "params": dict(f.params),
                 "evidence": [e.id for e in f.evidence],
                 "changes": [dataclasses.asdict(c) for c in f.changes],
                 "stage": reg.findings[f.code].stage if f.code in reg.findings else None}
            )  # fmt: skip
    plan = build_plan("file", findings, flight.params, dialect, reg)
    return {
        "file": path.name,
        "kind": params_file.kind,
        "vehicle": found,
        "firmware": firmware,
        "firmware_from": assumed,
        "defaults_from": defaults_from,
        "parameters": len(flight.params),
        "skipped": skipped,
        "findings": findings,
        "evidence": [dataclasses.asdict(e) for e in evidence.values()],
        "plan": to_json(plan, reg),
    }
