"""Run the analyzers on a flight and keep what they found (spec: Storage, "Analysis run").

A run records the engine version, the knowledge pack version and each analyzer's
version. While all three match, the stored run is the answer and nothing is
recomputed; when any changes, the flight is analysed again and a new run is added.
Deterministic results are a pure function of the log and these versions, so an old
run can always be reproduced.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Sequence
from typing import Any

from flight_insights import __version__
from flight_insights.analyzers.esc_rpm import ESC_RPM
from flight_insights.analyzers.filter_model import FILTER_MODEL
from flight_insights.analyzers.filters import FILTERS
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.log_adequacy import LOG_ADEQUACY
from flight_insights.analyzers.model import Analyzer, Evidence, Result
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.system import SYSTEM
from flight_insights.analyzers.vibration import VIBRATION
from flight_insights.knowledge import Pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store

log = logging.getLogger(__name__)

ANALYZERS: tuple[Analyzer, ...] = (
    LOG_ADEQUACY, SYSTEM, VIBRATION, ESC_RPM, FILTER_MODEL, FILTERS,
)  # fmt: skip


class AnalyzerBug(RuntimeError):
    """An analyzer emitted something the registry does not describe."""


def _versions(analyzers: Sequence[Analyzer]) -> dict[str, int]:
    return {a.name: a.version for a in analyzers}


def run_analysis(
    store: Store,
    cache: ColumnCache,
    pack: Pack,
    flight_id: str,
    analyzers: Sequence[Analyzer] = ANALYZERS,
    force: bool = False,
) -> dict[str, Any]:
    """The flight's analysis: the stored run if it is current, otherwise a new one."""
    reg = Registry.load(pack)
    if not force:
        latest = analysis_results(store, flight_id, reg, analyzers)
        if latest is not None and latest["current"]:
            return latest
    flight = FlightData.load(store, cache, pack, flight_id)
    outcomes: dict[str, dict[str, Any]] = {}
    results: list[tuple[Analyzer, Result]] = []
    for a in analyzers:
        try:
            r = a.run(flight, reg)
        except Exception as e:  # one analyzer failing leaves the others' results standing
            log.exception("analyzer %s failed on flight %s", a.name, flight_id)
            r = Result(skipped=f"failed: {type(e).__name__}: {e}")
        _check(a, r, reg)
        outcomes[a.name] = {"version": a.version, "skipped": r.skipped}
        results.append((a, r))
    evidence: dict[str, Evidence] = {}
    for _, r in results:
        for ev in [*r.evidence, *(e for f in r.findings for e in f.evidence)]:
            evidence[ev.id] = ev
    failed = any((o["skipped"] or "").startswith("failed") for o in outcomes.values())
    with store.transaction():
        run_id = store.insert(
            "analysis_run",
            {
                "flight_id": flight_id,
                "engine_version": __version__,
                "knowledge_version": reg.version,
                "analyzers_json": json.dumps(outcomes),
                "evidence_json": json.dumps([dataclasses.asdict(e) for e in evidence.values()]),
                "status": "partial" if failed else "complete",
            },
        )
        for a, r in results:
            for m in r.metrics:
                store.insert(
                    "metric_value",
                    {
                        "run_id": run_id,
                        "flight_id": flight_id,
                        "metric": m.metric,
                        "segment": m.segment,
                        "value": m.value,
                        "spread_json": json.dumps(dict(m.spread)),
                        "zone": m.zone,
                        "evidence_id": m.evidence_id,
                    },
                )
            for f in r.findings:
                store.insert(
                    "finding",
                    {
                        "run_id": run_id,
                        "flight_id": flight_id,
                        "analyzer": a.name,
                        "code": f.code,
                        "severity": f.severity,
                        "confidence": f.confidence,
                        "params_json": json.dumps(dict(f.params)),
                        "evidence_json": json.dumps([e.id for e in f.evidence]),
                        "changes_json": json.dumps([dataclasses.asdict(c) for c in f.changes]),
                    },
                )
    out = analysis_results(store, flight_id, reg, analyzers)
    assert out is not None
    return out


def _check(a: Analyzer, r: Result, reg: Registry) -> None:
    """What an analyzer emits must be declared by it and described in the registry."""
    for m in r.metrics:
        if m.metric not in a.metrics or m.metric not in reg.metrics:
            raise AnalyzerBug(f"{a.name} emitted metric {m.metric} it does not declare")
    for f in r.findings:
        if f.code not in a.findings or f.code not in reg.findings:
            raise AnalyzerBug(f"{a.name} emitted finding {f.code} it does not declare")
    cited = {e.id for e in r.evidence} | {e.id for f in r.findings for e in f.evidence}
    for m in r.metrics:
        if m.evidence_id not in cited:
            raise AnalyzerBug(f"{a.name}: metric {m.metric} cites missing evidence")


def analysis_results(
    store: Store,
    flight_id: str,
    reg: Registry,
    analyzers: Sequence[Analyzer] = ANALYZERS,
) -> dict[str, Any] | None:
    """The flight's latest run, with its metrics and findings, or None if it was never
    analysed. `current` says whether it was made with this engine and knowledge."""
    row = store.conn.execute(
        "SELECT id, engine_version, knowledge_version, analyzers_json, evidence_json, status, "
        "created_hlc FROM analysis_run WHERE flight_id = ? ORDER BY created_hlc DESC LIMIT 1",
        (flight_id,),
    ).fetchone()
    if row is None:
        return None
    run_id, engine, knowledge, outcomes_json, evidence_json, status, created = row
    outcomes: dict[str, dict[str, Any]] = json.loads(outcomes_json)
    current = (
        engine == __version__
        and knowledge == reg.version
        and {n: o["version"] for n, o in outcomes.items()} == _versions(analyzers)
    )
    metrics = [
        {"metric": m, "segment": s, "value": v, "spread": json.loads(sp), "zone": z, "evidence": e}
        for m, s, v, sp, z, e in store.conn.execute(
            "SELECT metric, segment, value, spread_json, zone, evidence_id FROM metric_value "
            "WHERE run_id = ? ORDER BY metric, segment",
            (run_id,),
        )
    ]
    findings = [
        {
            "id": fid,
            "analyzer": an,
            "code": code,
            "severity": sev,
            "confidence": conf,
            "params": json.loads(params),
            "evidence": json.loads(ev),
            "changes": json.loads(changes),
            "stage": reg.findings[code].stage if code in reg.findings else None,
        }
        for fid, an, code, sev, conf, params, ev, changes in store.conn.execute(
            "SELECT id, analyzer, code, severity, confidence, params_json, evidence_json, "
            "changes_json "
            "FROM finding WHERE run_id = ? ORDER BY "
            "CASE severity WHEN 'safety' THEN 0 WHEN 'out' THEN 1 WHEN 'caution' THEN 2 ELSE 3 END",
            (run_id,),
        )
    ]
    return {
        "run": run_id,
        "flight": flight_id,
        "created": created,
        "engine": engine,
        "knowledge": knowledge,
        "status": status,
        "current": current,
        "analyzers": outcomes,
        "metrics": metrics,
        "findings": findings,
        "evidence": json.loads(evidence_json),
    }
