"""Run the analyzers on a flight and keep what they found (spec: Storage, "Analysis run").

A run records the engine version, the knowledge pack version and each analyzer's
version. While all three match, the stored run is the answer and nothing is
recomputed; when any changes, the flight is analysed again and a new run is added.
Deterministic results are a pure function of the log and these versions, so an old
run can always be reproduced.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from flight_insights import __version__
from flight_insights.analyzers.buses import BUSES
from flight_insights.analyzers.esc_rpm import ESC_RPM
from flight_insights.analyzers.filter_model import FILTER_MODEL
from flight_insights.analyzers.filters import FILTERS
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.log_adequacy import LOG_ADEQUACY
from flight_insights.analyzers.model import Analyzer, Evidence, Result
from flight_insights.analyzers.motors_health import MOTORS_HEALTH
from flight_insights.analyzers.options import OPTIONS
from flight_insights.analyzers.params_audit import PARAMS_AUDIT
from flight_insights.analyzers.power import POWER
from flight_insights.analyzers.rate_health import RATE_HEALTH
from flight_insights.analyzers.rate_tune import RATE_TUNE
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.system import SYSTEM
from flight_insights.analyzers.tune import TUNE
from flight_insights.analyzers.vibration import VIBRATION
from flight_insights.knowledge import Pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store

log = logging.getLogger(__name__)

ANALYZERS: tuple[Analyzer, ...] = (
    LOG_ADEQUACY, SYSTEM, VIBRATION, ESC_RPM, FILTER_MODEL, FILTERS, POWER, MOTORS_HEALTH,
    BUSES, TUNE, RATE_TUNE, RATE_HEALTH, OPTIONS, PARAMS_AUDIT,
)  # fmt: skip


class AnalyzerBug(RuntimeError):
    """An analyzer emitted something the registry does not describe."""


def _versions(analyzers: Sequence[Analyzer]) -> dict[str, int]:
    return {a.name: a.version for a in analyzers}


def run_key(engine: str, knowledge: str, outcomes: Mapping[str, Mapping[str, Any]]) -> str:
    """What a run was made with, the same on every laptop: two runs with one key found
    the same things in the same log (spec: Shared storage and sync, portable references)."""
    made = {"engine": engine, "knowledge": knowledge,
            "analyzers": {n: o["version"] for n, o in sorted(outcomes.items())}}  # fmt: skip
    return hashlib.sha256(json.dumps(made, sort_keys=True).encode()).hexdigest()[:16]


def finding_keys(rows: Iterable[tuple[str, str, Sequence[str]]]) -> dict[str, str]:
    """A portable name for each finding of one run, from (id, code, evidence ids) in the
    order the analyzers emitted them. Finding ids are this laptop's own; evidence ids
    are labels (never measured numbers, which can differ in the last bit between
    machines), so code, evidence and the count of earlier twins name it everywhere."""
    seen: Counter[str] = Counter()
    out: dict[str, str] = {}
    for fid, code, evidence in rows:
        base = f"{code}|{','.join(sorted(evidence))}"
        seen[base] += 1
        digest = hashlib.sha256(f"{base}|{seen[base]}".encode()).hexdigest()[:12]
        out[fid] = f"{code}#{digest}"
    return out


def run_finding_keys(store: Store, run_id: str) -> dict[str, str]:
    return finding_keys(
        (fid, code, json.loads(ev))
        for fid, code, ev in store.conn.execute(
            "SELECT id, code, evidence_json FROM finding WHERE run_id = ? ORDER BY created_hlc",
            (run_id,),
        )
    )


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
        check_result(a, r, reg)
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


def declares(a: Analyzer, code: str) -> bool:
    """Whether the analyzer declares a finding code; "prefix.*" declares a family whose
    codes come from the knowledge pack (the Better options catalog)."""
    return any(code == d or (d.endswith(".*") and code.startswith(d[:-1])) for d in a.findings)


def check_result(a: Analyzer, r: Result, reg: Registry) -> None:
    """What an analyzer emits must be declared by it and described in the registry."""
    for m in r.metrics:
        if m.metric not in a.metrics or m.metric not in reg.metrics:
            raise AnalyzerBug(f"{a.name} emitted metric {m.metric} it does not declare")
    for f in r.findings:
        if not declares(a, f.code) or f.code not in reg.findings:
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
    keys = run_finding_keys(store, run_id)
    findings = [
        {
            "id": fid,
            "key": keys[fid],
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
        "run_key": run_key(engine, knowledge, outcomes),
        "status": status,
        "current": current,
        "analyzers": outcomes,
        "metrics": metrics,
        "findings": findings,
        "evidence": json.loads(evidence_json),
    }
