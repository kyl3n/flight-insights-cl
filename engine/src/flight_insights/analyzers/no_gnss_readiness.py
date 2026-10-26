"""Readiness for flying without GNSS, from the parameters and the platform setup (spec:
Flying without GNSS, "Readiness, from the parameters and the build"; rules in
`knowledge/rules/no_gnss.json`, readiness).

Each area gets a status and the facts behind it, as evidence `no_gnss:readiness:<area>`:
position, switching, heading, height, wind, failsafes and the pilot's fallback. A status is
ready, gap (something to fix), none (nothing set up) or unavailable (not in this firmware
build). Gaps a person has to fix are findings:

- no_gnss.source_not_fitted: a source set names a sensor that is not set up;
- no_gnss.no_source_switch: a source set for flying without GNSS that nothing selects;
- no_gnss.heading_needs_gnss: a heading that goes with GNSS (GPS yaw, or a compass yaw
  with no compass in use);
- no_gnss.drag_fusion_off: no wind estimate, so the estimator cannot dead-reckon and the
  dead-reckoning failsafe never acts (multirotors);
- no_gnss.no_pilot_fallback: no mode that needs no position on the transmitter's switches.

A parameter file is judged the same way as a log's parameters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, Finding, Result
from flight_insights.analyzers.registry import Registry

FIELDS = ("POSXY", "VELXY", "POSZ", "VELZ", "YAW")
HORIZONTAL = ("POSXY", "VELXY")
SENSOR_WORDS = {
    "beacon": "beacons", "optical_flow": "optical flow", "external_nav": "external navigation",
    "wheel_encoder": "wheel encoders", "rangefinder": "a downward rangefinder",
}  # fmt: skip
YAW_WORDS = {0: "none", 1: "compass", 2: "GPS", 3: "GPS with compass fallback",
             6: "external navigation", 8: "GSF"}  # fmt: skip
MODE_WORDS = {0: "Stabilize", 1: "Acro", 2: "AltHold", 13: "Sport", 22: "FlowHold"}
AUX_WORDS = {52: "Acro", 68: "Stabilize", 70: "AltHold", 71: "FlowHold"}
EKF_ACTION_WORDS = {0: "report only", 1: "Land", 2: "AltHold", 3: "Land from every mode"}
CHANNELS = range(1, 17)


@dataclass(frozen=True)
class SourceSet:
    """One estimator source set (EK3_SRCn_*), as the parameters set it."""

    n: int
    values: Mapping[str, int]

    def copes(self, rules: Mapping[str, Any]) -> bool:
        """Whether a horizontal source survives GNSS going (flow, vision, beacons...)."""
        sensors: Mapping[str, Mapping[str, str]] = rules["sensors"]
        return any(str(self.values.get(f)) in sensors[f] for f in HORIZONTAL)

    def sensors(self, rules: Mapping[str, Any]) -> list[str]:
        sensors: Mapping[str, Mapping[str, str]] = rules["sensors"]
        out: list[str] = []
        for f in FIELDS:
            s = sensors.get(f, {}).get(str(self.values.get(f)))
            if s and s not in out:
                out.append(s)
        return out


def source_sets(p: Mapping[str, float], rules: Mapping[str, Any]) -> list[SourceSet]:
    out: list[SourceSet] = []
    for n in rules["sets"]:
        values = {f: int(p[k]) for f in FIELDS if (k := f"EK3_SRC{n}_{f}") in p}
        if values:
            out.append(SourceSet(int(n), values))
    return out


def coping_sets(p: Mapping[str, float], rules: Mapping[str, Any]) -> list[SourceSet]:
    """The source sets that keep a horizontal source once GNSS goes."""
    return [s for s in source_sets(p, rules) if s.copes(rules)]


def _rangefinder_down(p: Mapping[str, float], rules: Mapping[str, Any]) -> bool:
    for name in rules["sensor_types"]["rangefinder"]:
        if p.get(name, 0):
            orient = p.get(name.replace("_TYPE", "_ORIENT"), rules["rangefinder_down"])
            if int(orient) == int(rules["rangefinder_down"]):
                return True
    return False


def fitted(p: Mapping[str, float], rules: Mapping[str, Any], sensor: str) -> bool:
    """Whether a sensor is set up (its type parameter set; a parameter this firmware does
    not have is a sensor it cannot use)."""
    if sensor == "rangefinder":
        return _rangefinder_down(p, rules)
    return any(p.get(name, 0) for name in rules["sensor_types"][sensor])


def missing(p: Mapping[str, float], rules: Mapping[str, Any], s: SourceSet
            ) -> list[tuple[str, str]]:  # fmt: skip
    """What a source set needs and does not have: (words, the parameter to set)."""
    out: list[tuple[str, str]] = []
    needed = s.sensors(rules)
    if "optical_flow" in needed and "rangefinder" not in needed:
        needed.append("rangefinder")  # flow needs its height above the ground
    for sensor in needed:
        if not fitted(p, rules, sensor):
            types: list[str] = rules["sensor_types"][sensor]
            out.append((SENSOR_WORDS[sensor], types[0].replace("1_", "n_")))
    nav = int(rules["flow_use_navigation"])
    if "optical_flow" in needed and int(p.get("EK3_FLOW_USE", nav)) != nav:
        out.append(("optical flow used for navigation", f"EK3_FLOW_USE {nav}"))
    return out


def channels(p: Mapping[str, float], *functions: int) -> list[int]:
    """The RC channels whose aux function is one of these."""
    return [c for c in CHANNELS if int(p.get(f"RC{c}_OPTION", 0)) in functions]


def drag_fusion(p: Mapping[str, float], rules: Mapping[str, Any]) -> bool | None:
    """Whether the estimator fuses drag to know the wind; None when this firmware build has
    no drag fusion (its parameters are absent)."""
    names = ("EK3_DRAG_BCOEF_X", "EK3_DRAG_BCOEF_Y", "EK3_DRAG_MCOEF")
    if not all(n in p for n in names):
        return None
    d = rules["drag"]
    return (p["EK3_DRAG_BCOEF_X"] > d["bcoef_min"] or p["EK3_DRAG_BCOEF_Y"] > d["bcoef_min"]
            or p["EK3_DRAG_MCOEF"] > d["mcoef_min"])  # fmt: skip


def compasses_in_use(p: Mapping[str, float]) -> int:
    if p.get("COMPASS_ENABLE", 1) == 0:
        return 0
    return sum(p.get(n) == 1 for n in ("COMPASS_USE", "COMPASS_USE2", "COMPASS_USE3"))


def _yaw_needs_gnss(s: SourceSet, rules: Mapping[str, Any], compasses: int) -> bool:
    yaw = s.values.get("YAW")
    if yaw is None:
        return False
    return yaw in rules["yaw"]["needs_gnss"] or (yaw in rules["yaw"]["compass"] and not compasses)


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


@dataclass
class _Out:
    result: Result
    p: Mapping[str, float]

    def area(self, name: str, status: str, summary: str, values: Mapping[str, Any],
             names: list[str]) -> Evidence:  # fmt: skip
        ev = Evidence(
            id=f"no_gnss:readiness:{name}", kind="parameter", summary=summary,
            values={"status": status, **values,
                    **{n: self.p.get(n) for n in names if n in self.p}},
        )  # fmt: skip
        self.result.evidence.append(ev)
        return ev

    def finding(self, code: str, severity: Any, ev: Evidence, params: Mapping[str, Any]) -> None:
        self.result.findings.append(Finding(code, severity, "high", (ev,), dict(params)))


def _readiness(flight: FlightData, reg: Registry) -> Result:
    p = flight.params
    if flight.vehicle != "copter" or not p:
        return Result(skipped="readiness without GNSS is judged for Copter parameters")
    rules: Mapping[str, Any] = reg.rules["no_gnss"]["readiness"]
    sets = source_sets(p, rules)
    if not sets or int(p.get("AHRS_EKF_TYPE", 3)) != 3:
        return Result(skipped="the estimator is not EKF3 with source sets")
    answers = flight.answers
    out = _Out(Result(), p)
    src_names = [f"EK3_SRC{s.n}_{f}" for s in sets for f in FIELDS]

    # --- position and velocity without GNSS ------------------------------------------------
    coping = [s for s in sets if s.copes(rules)]
    gaps = {s.n: m for s in sets if (m := missing(p, rules, s))}
    usable = [s for s in coping if s.n not in gaps]
    status = "ready" if usable else "gap" if coping else "none"
    words = (f"source set {_joined([str(s.n) for s in usable])} keeps a position without GNSS"
             if usable else
             f"source set {_joined([str(s.n) for s in coping])} names sensors not set up"
             if coping else "no estimator source set keeps a position without GNSS")  # fmt: skip
    pos = out.area("position", status, words, {
        "sets": [s.n for s in coping],
        "usable_sets": [s.n for s in usable],
        "sensors": sorted({x for s in coping for x in s.sensors(rules)}),
        "missing": [f"set {n}: {w} ({name})" for n, ms in gaps.items() for w, name in ms],
    }, [*src_names, "FLOW_TYPE", "EK3_FLOW_USE", "VISO_TYPE", "BCN_TYPE"])  # fmt: skip
    for n, ms in gaps.items():
        out.finding("no_gnss.source_not_fitted", "caution", pos,
                    {"set": n, "missing": ", ".join(name for _, name in ms)})  # fmt: skip

    # --- switching to it ---------------------------------------------------------------------
    first = next((s for s in sets if s.n == 1), None)
    needed = bool(coping) and not (first and first in coping)
    switch = channels(p, int(reg.rules["no_gnss"]["aux"]["source_set"]))
    scripting = bool(p.get("SCR_ENABLE", 0))
    status = "none" if not coping else "ready" if not needed or switch else "gap"
    words = ("source set 1 needs no switch" if coping and not needed
             else f"RC{switch[0]} selects the source set" if switch
             else "nothing selects the source set for flying without GNSS" if coping
             else "no source set to switch to")  # fmt: skip
    sw = out.area("switching", status, words,
                  {"needed": needed, "switch_channels": switch, "scripting": scripting},
                  ["SCR_ENABLE", "RC_OPTIONS"])  # fmt: skip
    if needed and not switch:
        target = _joined([str(s.n) for s in (usable or coping)])
        out.finding("no_gnss.no_source_switch", "caution", sw, {"set": target})

    # --- heading ---------------------------------------------------------------------------
    compasses = compasses_in_use(p)
    at_risk = [s for s in sets
               if (s.n == 1 or s in coping) and _yaw_needs_gnss(s, rules, compasses)]  # fmt: skip
    yaw = {s.n: YAW_WORDS.get(int(s.values.get("YAW", -1)), str(s.values.get("YAW")))
           for s in sets if "YAW" in s.values}  # fmt: skip
    words = (f"source set {_joined([str(s.n) for s in at_risk])} loses its heading with GNSS"
             if at_risk else "the heading does not depend on GNSS")  # fmt: skip
    head = out.area(
        "heading",
        "gap" if at_risk else "ready",
        words,
        {
            "yaw_sources": [f"set {n}: {w}" for n, w in yaw.items()],
            "compasses_in_use": compasses,
            "compassmot_done": bool(p.get("COMPASS_MOTCT", 0)),
        },
        [f"EK3_SRC{s.n}_YAW" for s in sets] + ["COMPASS_ENABLE", "COMPASS_USE", "COMPASS_MOTCT"],
    )
    for s in at_risk:
        out.finding("no_gnss.heading_needs_gnss", "caution", head,
                    {"set": s.n, "yaw": s.values.get("YAW"), "compasses": compasses})  # fmt: skip

    # --- height ------------------------------------------------------------------------------
    hgt = {1: "barometer", 2: "rangefinder", 3: "GPS", 4: "beacons", 6: "external navigation"}
    out.area("height", "ready",
             "height falls back to the barometer when its source is lost",
             {"height_sources": [f"set {s.n}: {hgt.get(s.values.get('POSZ', 0), 'none')}"
                                 for s in sets],
              "rangefinder_down": _rangefinder_down(p, rules)},
             [f"EK3_SRC{s.n}_POSZ" for s in sets])  # fmt: skip

    # --- wind --------------------------------------------------------------------------------
    frame: Mapping[str, Any] = flight.fingerprint.get("frame", {}).get("value") or {}
    heli = (frame.get("vehicle_class") == "helicopter"
            or int(p.get("FRAME_CLASS", 0)) in rules["helicopter_frames"])  # fmt: skip
    drag = None if heli else drag_fusion(p, rules)
    status = "ready" if drag else "unavailable" if drag is None else "gap"
    words = ("drag fusion gives the estimator the wind" if drag
             else "drag fusion is off: the estimator does not know the wind" if drag is False
             else "no drag fusion in this firmware build or frame")  # fmt: skip
    compensated = bool(p.get("BARO1_WCF_ENABLE", 0))
    wind = out.area("wind", status, words,
                    {"drag_fusion": drag, "baro_wind_compensation": compensated},
                    ["EK3_DRAG_BCOEF_X", "EK3_DRAG_BCOEF_Y", "EK3_DRAG_MCOEF"])  # fmt: skip
    if drag is False:
        severity = "caution" if answers.get("flies_without_gnss") is True else "info"
        out.finding("no_gnss.drag_fusion_off", severity, wind, {})

    # --- failsafes ---------------------------------------------------------------------------
    action = int(p.get("FS_EKF_ACTION", 1))
    ekf_on = action != 0 and p.get("FS_EKF_THRESH", 0.8) != 0
    out.area("failsafes", "ready" if ekf_on else "gap",
             f"on losing its position the EKF failsafe "
             f"{'switches to ' + EKF_ACTION_WORDS.get(action, '?') if ekf_on else 'does nothing'}",
             {"ekf_action": EKF_ACTION_WORDS.get(action, str(action)),
              "dead_reckoning_failsafe_acts": bool(drag) and bool(p.get("FS_DR_ENABLE", 0))},
             ["FS_EKF_ACTION", "FS_EKF_THRESH", "FS_DR_ENABLE", "FS_DR_TIMEOUT"])  # fmt: skip

    # --- the pilot's fallback ----------------------------------------------------------------
    flow = fitted(p, rules, "optical_flow")
    modes: list[int] = [*rules["modes_without_position"], *(rules["flow_modes"] if flow else [])]
    aux: list[int] = [*rules["aux_modes_without_position"],
                      *(rules["aux_flow_modes"] if flow else [])]  # fmt: skip
    on_switch: list[str] = []
    if int(p.get("FLTMODE_CH", 5)):
        for i in range(1, 7):
            m = p.get(f"FLTMODE{i}")
            if m is not None and int(m) in modes and MODE_WORDS[int(m)] not in on_switch:
                on_switch.append(MODE_WORDS[int(m)])
    for c in channels(p, *aux):
        word = AUX_WORDS[int(p[f"RC{c}_OPTION"])]
        if word not in on_switch:
            on_switch.append(word)
    station = bool(int(p.get("RC_OPTIONS", 0)) & 1)  # flown from the ground station
    drill = channels(p, int(reg.rules["no_gnss"]["aux"]["gps_disable"]))
    ready = bool(on_switch) or station
    words = (f"{_joined(on_switch)} on a switch" if on_switch
             else "modes change from the ground station" if station
             else "no mode that needs no position is on a switch")  # fmt: skip
    fallback = out.area("pilot_fallback", "ready" if ready else "gap", words,
                        {"modes": on_switch, "ground_station_control": station,
                         "drill_switch_channels": drill},
                        ["FLTMODE_CH", *(f"FLTMODE{i}" for i in range(1, 7))])  # fmt: skip
    if not ready:
        out.finding("no_gnss.no_pilot_fallback", "info", fallback, {})
    return out.result


NO_GNSS_READINESS = Analyzer(
    name="no_gnss_readiness",
    version=2,
    run=_readiness,
    metrics=(),
    findings=(
        "no_gnss.source_not_fitted", "no_gnss.no_source_switch", "no_gnss.heading_needs_gnss",
        "no_gnss.drag_fusion_off", "no_gnss.no_pilot_fallback",
    ),
)  # fmt: skip
