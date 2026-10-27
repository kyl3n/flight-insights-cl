"""The Navigation and GNSS view of one flight (spec: User interface, "The Navigation and
GNSS view"; mock-up Navigation): what disturbed GNSS, how the aircraft coped, which
evidence each receiver gave, and how ready it is to fly without GNSS.

It lays out what the flight's latest analysis already holds, read from the store:

- **GNSS events** (analyzers/gnss.py), each with its class, confidence, receivers,
  channels, peaks and the largest relative distances its evidence gives in metres;
- **outages and drills** (analyzers/no_gnss.py), each with what the estimator did;
- **how the aircraft coped** with each event or outage, as rows: the estimator, another
  position source, the position kept, the drift on return, the estimator's resets, the
  failsafes and how the failsafe analyzer judged them, the mode change, the heading and
  when GNSS was used again. Each row carries a status only from a verdict the engine
  already makes: a metric's zones, the readiness review's area status, the failsafe
  analyzer's verdict, the GNSS analyzer's heading-reset finding, or the drift the
  estimator followed without rejecting it (the spec's dangerous form). Otherwise it is a
  fact (info);
- **the readiness areas** (analyzers/no_gnss_readiness.py), each with its status, its
  values and which of the analyzer's sentences applies;
- **what would get it home:** the catalogue's mitigations that would change something
  on this flight's setup, ranked (mitigations.py): each with what it closes here, whether
  it makes the fleet goal reachable, its benefit measured on this airframe or its
  platform, else as the catalogue expects it, its limits, its sources, and the steps of
  the findings it answers. The setup findings that bear on getting home (the readiness
  review's, and the parameter checks' on the GNSS fallback, the second receiver, the
  compass and the dead-reckoning failsafe) keep their steps, each naming the
  mitigations that answer it; and the GNSS-off drill's steps, with whether the
  airframe's plan offers it (recommend/followup.py).

From the log it reads only which messages each receiver logged, never a value of
them. No position is read (local tracks stay with the analyzers), and every number in
the reply is one the analysis stored, in seconds after arming or relative metres; the
mitigations' holds and drift come from the platform's stored analyses in the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from flight_insights import mitigations
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import analysis_results
from flight_insights.knowledge import Pack
from flight_insights.recommend.plan import PLACEHOLDER, render
from flight_insights.store.db import Store

EVENT, OUTAGE, AREA = "gnss:event:", "no_gnss:outage:", "no_gnss:readiness:"
AREAS = ("position", "switching", "heading", "height", "wind", "failsafes", "pilot_fallback")
# the event's peaks, as the GNSS analyzer names them (gnss.PEAKS)
PEAKS = ("lowest_fix", "fewest_sats", "worst_hacc_m", "highest_cw", "worst_jam_state",
         "lowest_agc_ratio", "longest_time_stop_s", "highest_test_ratio",
         "largest_innovation_mps", "largest_time_jump_s")  # fmt: skip
# the setup findings that bear on getting home without GNSS
ACTIONS = ("no_gnss.source_not_fitted", "no_gnss.no_source_switch", "no_gnss.heading_needs_gnss",
           "no_gnss.drag_fusion_off", "no_gnss.no_pilot_fallback", "params.no_gnss_fallback",
           "params.second_gnss_unset", "params.compassmot_not_done",
           "params.dead_reckoning_failsafe_off")  # fmt: skip
# the failsafe analyzer's verdicts (analyzers/failsafes.py), in the status language
JUDGED = {"failsafe.as_configured": "ok", "failsafe.fell_back": "caution",
          "failsafe.unexpected": "out", "failsafe.late": "caution"}  # fmt: skip
ZONE = {"target": "ok", "caution": "caution", "out": "out"}
AREA_STATUS = {"ready": "ok", "gap": "caution", "none": "out", "unavailable": "info"}
WORST = ("out", "caution", "ok", "info")
# steps a procedure gives on its own: the rest come from the plan's own files
OWN_STEPS = ("task", "hardware", "firmware", "ground_check", "fly", "abort", "verify")

Values = Mapping[str, Any]


class NavigationError(ValueError):
    """No view: the flight was never analysed."""


def _ref(f: Mapping[str, Any]) -> dict[str, Any]:
    """A finding as the view gives it: the registry words it from its code and params."""
    return {k: f[k] for k in ("id", "code", "severity", "confidence", "params")}


def _row(row: str, status: str, verdict: str, **values: Any) -> dict[str, Any]:
    return {"id": row, "status": status, "verdict": verdict, "values": values}


def _zone(reg: Registry, metric: str, value: float | None) -> str:
    if value is None:
        return "info"
    return ZONE.get(reg.metrics[metric].zone(float(value)) or "", "info")


def _within(v: Values, start: float, end: float) -> bool:
    return start <= float(v["start_s"]) <= end or float(v["start_s"]) <= start <= float(v["end_s"])


class _Analysis:
    """The flight's latest analysis, indexed."""

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = result
        self.evidence: dict[str, Values] = {
            e["id"]: e.get("values", {}) for e in result["evidence"]
        }
        self.findings: list[Mapping[str, Any]] = list(result["findings"])

    def numbered(self, prefix: str) -> list[tuple[int, Values]]:
        """Evidence `<prefix><n>`, in order, without its parts (`<prefix><n>:drift`)."""
        out: list[tuple[int, Values]] = []
        for eid, v in self.evidence.items():
            rest = eid[len(prefix) :] if eid.startswith(prefix) else ""
            if rest.isdigit():
                out.append((int(rest), v))
        return sorted(out, key=lambda x: x[0])

    def citing(self, eid: str, codes: Callable[[str], bool]) -> Mapping[str, Any] | None:
        return next((f for f in self.findings if eid in f["evidence"] and codes(f["code"])), None)

    def judged(self, start: float, end: float) -> list[dict[str, Any]]:
        """The failsafe analyzer's verdicts on failsafes that fired from `start` to `end`."""
        out: list[dict[str, Any]] = []
        for f in self.findings:
            status = JUDGED.get(f["code"])
            at = self.evidence.get(f["evidence"][0], {}).get("at_s") if f["evidence"] else None
            if status is not None and at is not None and start <= float(at) <= end:
                out.append({"finding": _ref(f), "status": status})
        return out


# --- how the aircraft coped ------------------------------------------------------------------


def _source(area: Values, used: int | None) -> dict[str, Any]:
    """Another position source: one flown on, else the readiness review's position area."""
    if used is not None and used != 1:
        return _row("source", "ok", "used", set=used)
    if not area:  # the readiness review was skipped
        return _row("source", "info", "unknown")
    status = str(area["status"])
    return _row("source", AREA_STATUS.get(status, "info"), status,
                sets=area.get("usable_sets") or area.get("sets") or [],
                sensors=area.get("sensors", []), missing=area.get("missing", []))  # fmt: skip


def _hold(reg: Registry, o: Values) -> dict[str, Any]:
    kept = o.get("kept_s")
    verdict = "held" if o.get("held") else "relative" if o.get("kept_relative") else "lost"
    return _row("hold", _zone(reg, "no_gnss.hold_s", kept), verdict, kept_s=kept,
                absolute_s=o.get("absolute_s"), duration_s=o.get("duration_s"))  # fmt: skip


def _drift(reg: Registry, d: Values) -> dict[str, Any]:
    rate = d.get("drift_m_per_min")
    return _row("drift", _zone(reg, "no_gnss.drift_rate", rate), "measured",
                drift_m=d.get("drift_m"), rate=rate,
                minutes=d.get("minutes_to_tolerance"))  # fmt: skip


def _failsafes(judged: list[dict[str, Any]], names: Sequence[str]) -> dict[str, Any]:
    if judged:  # with every failsafe and check the log names, judged or not
        status = min((j["status"] for j in judged), key=WORST.index)
        return _row("failsafe", status, "judged", judged=judged, names=list(names))
    if names:
        return _row("failsafe", "info", "named", names=list(names))
    return _row("failsafe", "info", "none")


def _mode(v: Values) -> list[dict[str, Any]]:
    if not v.get("mode"):
        return []
    return [_row("mode", "info", "changed", mode=v["mode"], reason=v.get("mode_reason"))]


def _heading(area: Values, yaw: Values | None, spoofing: bool) -> dict[str, Any]:
    """A heading reset from GNSS velocity during spoofing (the GNSS analyzer's safety
    finding), else the readiness review's heading area."""
    if yaw is not None:
        return _row("heading", "out", "reset", count=yaw.get("yaw_resets"),
                    first_s=yaw.get("first_s"))  # fmt: skip
    # during spoofing the GNSS analyzer looked for resets and found none
    if not area:  # the readiness review was skipped
        return _row("heading", "info", "unknown", no_reset=spoofing)
    status = str(area["status"])
    return _row("heading", AREA_STATUS.get(status, "info"), status,
                compasses=area.get("compasses_in_use"), no_reset=spoofing)  # fmt: skip


def _event_rows(
    a: _Analysis, reg: Registry, n: int, v: Values, outages: list[dict[str, Any]],
    areas: Mapping[str, Values],
) -> list[dict[str, Any]]:  # fmt: skip
    rows: list[dict[str, Any]] = []
    if v.get("followed_drift"):
        rows.append(_row("estimator", "out", "followed",
                         innovation=v.get("largest_innovation_mps")))  # fmt: skip
    elif v.get("rejected_s"):
        rows.append(_row("estimator", "info", "rejected", rejected_s=v["rejected_s"],
                         ratio=v.get("highest_test_ratio")))  # fmt: skip
    else:
        rows.append(_row("estimator", "info", "not_rejected"))
    used = next((o["source_set"] for o in outages if o.get("source_set")), None)
    rows.append(_source(areas.get("position", {}), used))
    if outages:  # the shortest it kept a position for, while GNSS was gone
        rows.append(_hold(reg, min(outages, key=lambda o: float(o.get("kept_s") or 0))))
        rows += [_drift(reg, o["drift"]) for o in outages if o.get("drift")][:1]
    position = a.evidence.get(f"{EVENT}{n}:position")
    if position and position.get("estimator_resets"):
        rows.append(_row("resets", "info", "reset", count=position["estimator_resets"],
                         largest_m=position.get("largest_reset_m")))  # fmt: skip
    response = float(reg.rules["gnss"]["response_s"])
    judged = a.judged(float(v["start_s"]), float(v["end_s"]) + response)
    rows.append(_failsafes(judged, v.get("failsafes") or []))
    rows += _mode(v)
    yaw = a.evidence.get(f"{EVENT}{n}:yaw")
    rows.append(_heading(areas.get("heading", {}), yaw, v.get("class") == "spoofing"))
    return rows


def _outage_rows(
    a: _Analysis, reg: Registry, o: dict[str, Any], areas: Mapping[str, Values]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if o.get("using_gnss_at_start") is False:
        rows.append(_row("estimator", "info", "not_using", set=o.get("source_set")))
    elif o.get("detection_s") is not None:
        rows.append(_row("estimator", _zone(reg, "no_gnss.detection_s", o["detection_s"]),
                         "stopped", detection_s=o["detection_s"]))  # fmt: skip
    else:
        rows.append(_row("estimator", "info", "kept_using"))
    used = o.get("source_set") if o.get("using_gnss_at_start") is False else None
    rows.append(_source(areas.get("position", {}), used))
    rows.append(_hold(reg, o))
    if o.get("drift"):
        rows.append(_drift(reg, o["drift"]))
    response = float(reg.rules["no_gnss"]["response_s"])
    judged = a.judged(float(o["start_s"]), float(o["end_s"]) + response)
    rows.append(_failsafes(judged, o.get("failsafes") or []))
    rows += _mode(o)
    if o.get("resumed_s") is not None:
        rows.append(_row("resumed", "info", "resumed", resumed_s=o["resumed_s"]))
    return rows


# --- the parts ---------------------------------------------------------------------------------


def _outages(a: _Analysis, reg: Registry) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, v in a.numbered(OUTAGE):
        eid = f"{OUTAGE}{n}"
        finding = a.citing(eid, lambda c: c.startswith("no_gnss.position_"))
        keys = ("start_s", "end_s", "duration_s", "drill", "using_gnss_at_start", "detection_s",
                "absolute_s", "kept_s", "held", "kept_relative", "resumed_s", "source_set",
                "failsafes", "mode", "mode_reason")  # fmt: skip
        o: dict[str, Any] = {"n": n, **{k: v.get(k) for k in keys}}
        o["drift"] = a.evidence.get(f"{eid}:drift")
        o["finding"] = _ref(finding) if finding else None
        o["evidence"] = [e for e in (eid, f"{eid}:drift") if e in a.evidence]
        out.append(o)
    return out


def _events(a: _Analysis, reg: Registry, outages: list[dict[str, Any]],
            areas: Mapping[str, Values]) -> list[dict[str, Any]]:  # fmt: skip
    out: list[dict[str, Any]] = []
    for n, v in a.numbered(EVENT):
        eid = f"{EVENT}{n}"
        finding = a.citing(eid, lambda c: c.startswith("gnss.") and c != "gnss.yaw_reset")
        yaw = a.citing(f"{eid}:yaw", lambda c: c == "gnss.yaw_reset")
        during = [o for o in outages if _within(o, float(v["start_s"]), float(v["end_s"]))]
        position = a.evidence.get(f"{eid}:position")
        altitude = a.evidence.get(f"{eid}:altitude")
        time = a.evidence.get(f"{eid}:time")
        out.append({
            "n": n,
            **{k: v.get(k) for k in ("class", "confidence", "start_s", "end_s", "duration_s",
                                     "receivers", "channels")},
            "peaks": {k: v[k] for k in PEAKS if v.get(k) is not None},
            "position": dict(position) if position else None,
            "altitude_gap_m": altitude.get("largest_altitude_gap_m") if altitude else None,
            "time": dict(time) if time else None,
            "finding": _ref(finding) if finding else None,
            "heading_finding": _ref(yaw) if yaw else None,
            "outages": [o["n"] for o in during],
            "coping": _event_rows(a, reg, n, v, during, areas),
            "evidence": [e for e in a.evidence if e == eid or e.startswith(f"{eid}:")],
        })  # fmt: skip
    for o in outages:  # each outage names the events it fell in, and how it coped
        o["events"] = [e["n"] for e in out if o["n"] in e["outages"]]
        o["coping"] = _outage_rows(a, reg, o, areas)
    return out


def _say(area: str, v: Values, a: _Analysis) -> tuple[str, dict[str, Any]]:
    """Which of the readiness analyzer's sentences applies to the area, and what fills it
    (no_gnss_readiness.py words each area the same way, in English, as evidence)."""
    status = str(v.get("status"))
    if area == "switching":
        switch: list[int] = list(v.get("switch_channels") or [])
        coping = status != "none"
        if coping and not v.get("needed"):
            return "not_needed", {}
        if switch:  # a switch with no set to change to says so
            return ("switch" if coping else "switch_nothing"), {"channel": switch[0]}
        return ("gap", {}) if coping else ("none", {})
    if area == "heading" and status == "gap":
        sets = [
            f["params"].get("set") for f in a.findings if f["code"] == "no_gnss.heading_needs_gnss"
        ]
        return "gap", {"sets": sets}
    if area == "height":
        return "barometer", {}
    if area == "wind":
        drag = v.get("drag_fusion")
        return ("on" if drag else "unavailable" if drag is None else "off"), {}
    if area == "failsafes":
        return ("ekf" if status == "ready" else "ekf_off"), {}
    if area == "pilot_fallback":
        if v.get("modes"):
            return "modes", {}
        return ("station" if v.get("ground_station_control") else "none"), {}
    return status, {}


def _readiness(a: _Analysis) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for area in AREAS:
        v = a.evidence.get(f"{AREA}{area}")
        if v is None:
            continue
        say, fill = _say(area, v, a)
        out.append({"area": area, "status": v.get("status"), "say": say, "fill": fill,
                    "values": dict(v), "evidence": f"{AREA}{area}"})  # fmt: skip
    return out


def _steps(reg: Registry, code: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A finding's procedure's own steps, filled from its params (the plan adds the
    parameter files and their reboots); a step its finding cannot fill is left out."""
    steps: list[dict[str, Any]] = []
    for s in reg.procedures.get(code, {}).get("steps", ()):
        if s["kind"] not in OWN_STEPS or s.get("when") is not None:
            continue
        text = {lang: render(t, params) for lang, t in s["text"].items()}
        if not PLACEHOLDER.search(text["en"]):
            steps.append({"kind": s["kind"], "text": text})
    return steps


def _actions(a: _Analysis, reg: Registry) -> list[dict[str, Any]]:
    """The setup findings that bear on getting home, as the analysis orders them, each with
    its procedure's own steps."""
    return [
        {"finding": _ref(f), "steps": _steps(reg, f["code"], f["params"]), "mitigations": []}
        for f in a.findings
        if f["code"] in ACTIONS
    ]


def _mitigations(
    store: Store, pack: Pack, reg: Registry, a: _Analysis, flight: FlightData,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:  # fmt: skip
    """The catalogue ranked for this flight's setup (mitigations.rank), each with the
    findings it answers and their steps; each action names the mitigations answering it."""
    doc = mitigations.load(pack, reg)
    areas = {area: a.evidence[f"{AREA}{area}"] for area in AREAS if f"{AREA}{area}" in a.evidence}
    setup = mitigations.Setup(areas, a.findings, flight.params, flight.answers)
    airframe = flight.airframe_id
    outages = mitigations.fleet_outages(store, reg, airframe) if airframe else []
    goal = mitigations.goal_check(
        reg.rules["failsafes"], pack.json("recovery/copter.json")["link"]["defaults"]
    )
    ranked = mitigations.rank(
        doc, setup, reg.rules["no_gnss"]["readiness"], goal, outages, airframe
    )
    by_id = {f["id"]: f for f in a.findings}
    for item in ranked["items"]:
        answered = [by_id[i] for i in item["closes"]["findings"]]
        item["closes"]["findings"] = [_ref(f) for f in answered]
        seen: set[str] = set()
        item["steps"] = []
        for f in answered:
            for step in _steps(reg, f["code"], f["params"]):
                if step["text"]["en"] not in seen:
                    seen.add(step["text"]["en"])
                    item["steps"].append(step)
        for action in actions:
            if action["finding"]["id"] in {f["id"] for f in answered}:
                action["mitigations"].append(item["id"])
    drill: Mapping[str, Any] = doc["drill"]
    drilled = sorted({o.flight for o in outages if o.drill and o.airframe == airframe})
    offered = bool(airframe) and not drilled and f"{AREA}pilot_fallback" in a.evidence
    sources: Mapping[str, Mapping[str, Any]] = doc["sources"]
    return {
        "goal_now": ranked["goal_now"],
        "items": ranked["items"],
        "drill": {
            "about": drill["about"],
            "measured": drilled,
            "offered": offered,
            "steps": _steps(reg, str(drill["procedure"]), {}),
            "sources": [{"id": s, **sources[s]} for s in drill["sources"]],
        },
    }


def _logged(table: Any, instance_column: str, instance: int, column: str) -> bool:
    if table is None or table.num_rows == 0 or column not in table.column_names:
        return False
    if instance_column not in table.column_names:
        return instance == 0
    rows = np.asarray(table.column(instance_column).to_numpy(zero_copy_only=False)) == instance
    return bool(np.any(rows))


def _receivers(flight: FlightData, pack: Pack) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Which evidence each receiver logged: never a value, only whether it is there."""
    gps = flight.table("GPS", ["TimeUS", "I", "Status", "GWk"])
    gpa = flight.table("GPA", ["TimeUS", "I", "HAcc"])
    ubx = flight.table("UBX1", ["TimeUS", "Instance", "jamInd"])
    instances: list[int] = []
    if gps is not None and gps.num_rows and "Status" in gps.column_names:
        raw = gps.column("I").to_numpy(zero_copy_only=False) if "I" in gps.column_names else [0]
        instances = sorted({int(i) for i in np.unique(np.asarray(raw))})
    meta = pack.metadata(pack.choose(flight.firmware or "0").version, flight.vehicle or "copter")
    out: list[dict[str, Any]] = []
    for i in instances:
        name = f"GPS{i + 1}_TYPE"
        value = flight.params.get(name)
        kind = None
        if value is not None:
            kind = meta.get(name, {}).get("values", {}).get(str(int(value)), "?")
        timed = False
        if gps is not None and "GWk" in gps.column_names:
            rows = np.asarray(gps.column("I").to_numpy(zero_copy_only=False)) == i
            weeks = np.asarray(gps.column("GWk").to_numpy(zero_copy_only=False))[rows]
            timed = bool(np.any(weeks > 0))  # GPS time, once it has a week
        out.append({
            "instance": i + 1, "param": name, "type": kind,
            "logged": {
                "ublox_status": _logged(ubx, "Instance", i, "jamInd"),
                "fix": True,
                "accuracy": _logged(gpa, "I", i, "HAcc"),
                "gps_time": timed,
            },
        })  # fmt: skip
    x3, x4 = flight.table("XKF3", ["TimeUS", "IVN"]), flight.table("XKF4", ["TimeUS", "SP"])
    baro = flight.table("BARO", ["TimeUS", "Alt"])
    independent = {
        "estimator": x3 is not None and x3.num_rows > 0 and x4 is not None and x4.num_rows > 0,
        "barometer": baro is not None and baro.num_rows > 0,
        "second_receiver": len(instances) >= 2,
    }
    return out, independent


def navigation(
    store: Store,
    pack: Pack,
    reg: Registry,
    flight_id: str,
    open_flight: Callable[[str], FlightData],
) -> dict[str, Any]:
    """The view of one flight, from its latest analysis and which messages its log holds."""
    row = store.get("flight", flight_id)
    if row is None:
        raise NavigationError("no such flight")
    result = analysis_results(store, flight_id, reg)
    if result is None:
        raise NavigationError("the flight has not been analysed yet")
    a = _Analysis(result)
    readiness = _readiness(a)
    areas = {r["area"]: r["values"] for r in readiness}
    outages = _outages(a, reg)
    events = _events(a, reg, outages, areas)
    flight = open_flight(flight_id)
    receivers, independent = _receivers(flight, pack)
    outcomes: Mapping[str, Mapping[str, Any]] = result["analyzers"]
    skipped = {k: outcomes[k]["skipped"] if k in outcomes else None
               for k in ("gnss", "no_gnss", "no_gnss_readiness")}  # fmt: skip
    actions = _actions(a, reg)
    return {
        "flight": flight_id,
        "airframe_id": row["airframe_id"],
        "current": result["current"],
        "skipped": skipped,
        "events": events,
        "outages": outages,
        "receivers": receivers,
        "independent": independent,
        "readiness": readiness,
        "mitigations": _mitigations(store, pack, reg, a, flight, actions),
        "actions": actions,
        "sources": list(reg.rules["no_gnss"]["readiness"]["sources"]),
    }
