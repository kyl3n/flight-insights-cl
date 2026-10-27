"""Parameter audit (spec: Analyzers common to all vehicles, "Parameter audit";
Configuration review, "What the review finds").

Every parameter gets one of the four verdicts:

- default: the value is the firmware's own default, as the log recorded it;
- explained: changed, for a reason the build explains: a calibration, a learned value,
  a counter, hardware identity, or a parameter the fingerprint reads to describe the
  aircraft (frame, motor protocol, battery, sensors);
- unexplained: a tuning parameter changed with no evident reason;
- inconsistent: the value contradicts its documentation or the aircraft.

A parameter whose default the source did not record is counted apart, as is one this
firmware does not know (a custom build adds its own: unknown, not wrong).

Findings report what needs a person: values outside the documented range, failsafes
with no action (on every battery monitor), FS_THR_VALUE breaking the arming rule, battery
and voltage-scaling settings made for another pack, a capacity that is not the pack's,
arming that never checks the pack's charge, a power module never calibrated, and rate
gains never tuned.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Mapping
from typing import Any, cast

from flight_insights.analyzers.facts import CELL_VOLTS, MONITORS
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, CandidateChange, Evidence, Finding, Result
from flight_insights.analyzers.no_gnss_readiness import coping_sets, drag_fusion, source_sets
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import KnowledgeError
from flight_insights.params.dialect import Dialect
from flight_insights.params.files import format_value
from flight_insights.params.validate import metadata_problem

UNJUDGED = ("counter", "ground station")  # values that change on their own, never set wrong
PARAM_NAME = re.compile(r"\b[A-Z][A-Z0-9_]{1,15}\*?")
TOLERANCE = 1e-6


def _fingerprint_params(fp: Mapping[str, Any], params: Mapping[str, float]) -> set[str]:
    """Parameters the fingerprint reads to describe the aircraft, from each attribute's
    source ("FRAME_CLASS, FRAME_TYPE", "EK3_SRC1_*")."""
    names: set[str] = set()
    for attr in fp.values():
        source = cast(Mapping[str, Any], attr).get("source") if isinstance(attr, dict) else None
        if not isinstance(source, str):
            continue
        for token in PARAM_NAME.findall(source):
            if token.endswith("*"):
                names |= {p for p in params if p.startswith(token[:-1])}
            elif token in params:
                names.add(token)
    return names


# Calibration values and the device ID that says whether a sensor fills their slot:
# INS_ACCSCAL_X and INS_ACC_ID, INS_ACC2OFFS_X and INS_ACC2_ID, INS4_GYROFFS_X and
# INS4_GYR_ID; COMPASS_OFS2_X and COMPASS_DEV_ID2.
INS_CALIBRATION = re.compile(r"^INS(\d?)_(ACC|GYR)(\d?)(?:SCAL|OFFS)_[XYZ]$")
COMPASS_CALIBRATION = re.compile(r"^COMPASS_(?:DIA|ODI|OFS|MOT|SCALE)(\d?)(?:_[XYZ])?$")


def _absent_sensor(name: str, params: Mapping[str, float]) -> bool:
    """A calibration value for a sensor slot whose device ID is 0: no sensor is there."""
    if m := INS_CALIBRATION.match(name):
        block, kind, n = m.groups()
        device = f"INS{block}_{kind}_ID" if block else f"INS_{kind}{n}_ID"
    elif m := COMPASS_CALIBRATION.match(name):
        device = f"COMPASS_DEV_ID{m.group(1)}"
    else:
        return False
    return params.get(device) == 0


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _is_default(name: str, value: float, defaults: Mapping[str, float]) -> bool:
    d = defaults.get(name)
    return d is not None and abs(d - value) <= TOLERANCE * max(1.0, abs(value))


def ev(eid: str, summary: str, values: Mapping[str, Any]) -> Evidence:
    return Evidence(id=eid, kind="parameter", summary=summary, values=dict(values))


def _cells(flight: FlightData) -> float | None:
    entry: Mapping[str, Any] = flight.fingerprint.get("cells") or {}
    cells = entry.get("value")
    return float(cells) if cells else None


def _per_cell(value: float, cells: float) -> str:
    return format_value(round(value / cells, 2))


def _audit(flight: FlightData, reg: Registry) -> Result:
    vehicle = flight.vehicle
    if vehicle not in ("copter", "plane") or not flight.params:
        return Result(skipped="no parameters, or a vehicle the audit does not know")
    rules: Mapping[str, Any] = reg.rules["audit"]
    try:
        dialect = Dialect(reg.pack, vehicle, flight.firmware or "0")
    except KnowledgeError as e:
        return Result(skipped=f"no parameter metadata: {e}")
    p, defaults = flight.params, flight.defaults
    firmware = flight.firmware or dialect.choice.version
    out = Result()

    def native(name: str) -> str:
        return dialect.native(name)[0]

    # --- values against the firmware's own documentation --------------------------------
    unknown: list[str] = []
    problems: dict[str, dict[str, Any]] = {}
    undefined: dict[str, tuple[int, int]] = {}  # bitmask: (value, value with only defined bits)
    # The simulator's own settings describe no aircraft.
    judged = {n: v for n, v in p.items() if not (flight.simulated and n.startswith("SIM_"))}
    for name, value in judged.items():
        nname, nvalue = dialect.native(name, value)
        meta = dialect.meta.get(nname)
        if meta is None:
            unknown.append(nname)
            continue
        if _is_default(name, value, defaults) or dialect.param_class(nname, p) in UNJUDGED:
            continue  # the firmware chose it, or it counts on its own: nobody set it wrong
        if _absent_sensor(name, p):
            continue  # a calibration slot with no sensor behind it holds no real value
        problem = metadata_problem(nname, value if nvalue is None else nvalue, meta, firmware)
        if problem is not None and problem[0] == "out_of_range" and name in defaults:
            # a documented range the firmware's own default falls outside is no real bound
            default = metadata_problem(nname, defaults[name], meta, firmware)
            if default is not None and default[0] == "out_of_range":
                continue
        if problem is not None and problem[0] == "undefined_bits" and value == int(value) >= 0:
            # bits the firmware does not define do nothing: worth clearing, never a hold-up
            bits: Mapping[str, str] = meta.get("bitmask") or {}
            defined = sum(1 << int(b) for b in bits)
            undefined[name] = (int(value), int(value) & defined)
            continue
        if problem is not None and problem[0] != "read_only":
            problems[name] = {"reason": problem[0], **problem[1]}

    listed = int(rules["listed_names_max"])
    # Controller gains and limits belong to the tuning stages, after the filters; the rest
    # is setup. Each group is its own finding, so one never holds the other back.
    controller = [n for n in problems if _matches(n, rules["controller_names"])]
    for code, group in (
        ("params.out_of_range", [n for n in problems if n not in controller]),
        ("params.gain_out_of_range", controller),
    ):
        if not group:
            continue
        names = sorted(native(n) for n in group)
        out.findings.append(
            Finding(
                code=code,
                severity="caution",
                confidence="high",
                evidence=(
                    ev(
                        f"{code.replace('.', ':')}",
                        "values against the firmware's metadata",
                        {native(n): problems[n] for n in group},
                    ),
                ),
                params={"count": len(names), "names": names[:listed], "firmware": firmware},
            )
        )

    if undefined:
        names = sorted(native(n) for n in undefined)
        out.findings.append(
            Finding(
                code="params.undefined_bits",
                severity="info",
                confidence="high",
                evidence=(
                    ev(
                        "params:undefined-bits",
                        "bitmask values against the bits the firmware defines",
                        {
                            native(n): {"value": v, "defined_only": k}
                            for n, (v, k) in undefined.items()
                        },
                    ),
                ),
                params={"names": names[:listed], "firmware": firmware},
                changes=tuple(
                    CandidateChange(n, float(v), float(k), "setup")
                    for n, (v, k) in undefined.items()
                ),
            )
        )

    contradicted: set[str] = set(problems) | set(undefined)
    if vehicle == "copter":
        readiness: Mapping[str, Any] = reg.rules["no_gnss"]["readiness"]
        contradicted |= _failsafes(flight, rules, dialect.meta, readiness, out)
        contradicted |= _battery(flight, rules, out)
        contradicted |= _other_monitors(flight, out)
        contradicted |= _power_setup(flight, rules, out)
        _navigation(flight, readiness, out)
        _never_tuned(flight, rules, out)

    # --- the four verdicts ----------------------------------------------------------------
    by_fingerprint = _fingerprint_params(flight.fingerprint, p)
    verdicts: dict[str, list[str]] = {
        "default": [], "explained": [], "unexplained": [], "inconsistent": [],
        "default_unknown": [],
    }  # fmt: skip
    for name, value in judged.items():
        nname = native(name)
        if nname in unknown:
            continue
        if name in contradicted:
            verdict = "inconsistent"
        elif name not in defaults:
            verdict = "default_unknown"
        elif _is_default(name, value, defaults):
            verdict = "default"
        elif name in by_fingerprint or dialect.param_class(nname, flight.params) != "tuning":
            verdict = "explained"
        else:
            verdict = "unexplained"
        verdicts[verdict].append(nname)
    audit = ev(
        "params:audit",
        "every parameter's verdict: default, explained, unexplained or inconsistent",
        {
            "counts": {k: len(v) for k, v in verdicts.items()} | {"unknown": len(unknown)},
            "unexplained": sorted(verdicts["unexplained"]),
            "inconsistent": sorted(verdicts["inconsistent"]),
            "unknown": sorted(unknown),
            "metadata_version": dialect.choice.version,
        },
    )
    out.evidence.append(audit)
    if verdicts["unexplained"]:
        names = sorted(verdicts["unexplained"])
        out.findings.append(
            Finding(
                code="params.unexplained",
                severity="info",
                confidence="medium",
                evidence=(audit,),
                params={"count": len(names), "names": names[:listed]},
            )
        )
    return out


def _failsafes(
    flight: FlightData, rules: Mapping[str, Any], meta: Mapping[str, Any],
    readiness: Mapping[str, Any], out: Result,
) -> set[str]:  # fmt: skip
    p = flight.params
    flagged: set[str] = set()

    def add(
        code: str, severity: Any, names: list[str], params: Mapping[str, Any],
        changes: tuple[CandidateChange, ...] = (),
    ) -> None:  # fmt: skip
        values = {n: p.get(n) for n in names}
        out.findings.append(
            Finding(
                code=code,
                severity=severity,
                confidence="high",
                evidence=(ev(f"params:{code}", "the failsafe parameters", values),),
                params=dict(params),
                changes=changes,
            )
        )
        flagged.update(names)

    def back_to_default(name: str) -> tuple[CandidateChange, ...]:
        default = flight.defaults.get(name)
        return (CandidateChange(name, p.get(name), default, "operations"),) if default else ()

    if p.get("FS_THR_ENABLE") == 0:
        add("params.rc_failsafe_off", "safety", ["FS_THR_ENABLE"], {})
    elif "FS_THR_VALUE" in p:
        fs = rules["fs_thr"]
        channel = int(p.get("RCMAP_THROTTLE", 3))
        rc_min_param = f"RC{channel}_MIN"
        rc_min, value = p.get(rc_min_param), p["FS_THR_VALUE"]
        if value < fs["min_value"] or (
            rc_min is not None and rc_min <= value + fs["margin_below_rc_min"]
        ):
            add(
                "params.fs_thr_value", "out", ["FS_THR_VALUE", rc_min_param],
                {"value": format_value(value), "rc_min_param": rc_min_param,
                 "rc_min": "?" if rc_min is None else format_value(rc_min)},
            )  # fmt: skip
    if p.get("FS_EKF_ACTION") == 0:
        add("params.ekf_failsafe_report_only", "caution", ["FS_EKF_ACTION"], {})
    if p.get("FS_CRASH_CHECK") == 0:
        add("params.crash_check_off", "caution", ["FS_CRASH_CHECK"], {})
    # Failsafes the firmware ships switched on, switched off: back to the firmware's own
    # value (from the log's defaults; none proposed where the defaults are unknown). The
    # dead-reckoning one can only act while drag fusion gives the estimator the wind.
    for name, code in (
        ("FS_VIBE_ENABLE", "params.vibe_failsafe_off"),
        ("FS_DR_ENABLE", "params.dead_reckoning_failsafe_off"),
        ("FS_EKF_THRESH", "params.ekf_failsafe_off"),
    ):
        if name == "FS_DR_ENABLE" and not drag_fusion(p, readiness):
            continue
        if p.get(name) == 0:
            add(code, "caution", [name], {}, back_to_default(name))
    # Winning the radio link back comes first (platform setup): dead reckoning keeps
    # flying the failsafe home until the battery says otherwise, not for FS_DR_TIMEOUT's
    # usual half minute.
    want = float(rules["dr_timeout"]["win_link_back_s"])
    if (
        flight.answers.get("link_loss") == "win_link_back"
        and drag_fusion(p, readiness)
        and p.get("FS_DR_ENABLE", 0) != 0
        and p.get("FS_DR_TIMEOUT", want) < want
    ):
        add(
            "params.dead_reckoning_timeout_short", "caution", ["FS_DR_TIMEOUT", "FS_DR_ENABLE"],
            {"value": format_value(p["FS_DR_TIMEOUT"]), "proposed": format_value(want)},
            (CandidateChange("FS_DR_TIMEOUT", p["FS_DR_TIMEOUT"], want, "operations"),),
        )  # fmt: skip
    # Flown from the ground station alone (RC_OPTIONS bit 0 ignores the receiver), the
    # GCS link is the only one, and losing it must do something.
    if int(p.get("RC_OPTIONS", 0)) & 1 and p.get("FS_GCS_ENABLE") == 0:
        add("params.gcs_failsafe_off", "safety", ["FS_GCS_ENABLE", "RC_OPTIONS"], {})
    skipped = int(p.get("ARMING_SKIPCHK", 0))
    if skipped:
        entry: Mapping[str, Any] = meta.get("ARMING_SKIPCHK") or {}
        names: Mapping[str, str] = entry.get("bitmask") or {}
        checks = [str(names.get(str(b), f"bit {b}")) for b in range(32) if skipped >> b & 1]
        add(
            "params.arming_checks_skipped", "caution", ["ARMING_SKIPCHK"],
            {"checks": ", ".join(checks)},
            (CandidateChange("ARMING_SKIPCHK", float(skipped), 0.0, "operations"),),
        )  # fmt: skip
    return flagged


def _starting_volts(flight: FlightData, rules: Mapping[str, Any]) -> dict[str, float]:
    """Low, critical and arming voltages for the pack from Mission Planner's initial
    parameters; empty without the cell count and the chemistry."""
    cells = _cells(flight)
    volts = CELL_VOLTS.get(str(flight.answers.get("battery_chemistry") or ""))
    if not cells or volts is None:
        return {}
    r: Mapping[str, float] = rules["starting_volts"]
    cell_min = volts[1]
    return {
        "low": round((cell_min + r["low_above_min"]) * cells, 2),
        "critical": round((cell_min + r["critical_above_min"]) * cells, 2),
        "arm": round(
            (cells - 1) * r["arm_per_extra_cell"] + (cell_min + r["arm_above_min"]) * cells, 2
        ),
    }


def _set(name: str, p: Mapping[str, float], value: float | None) -> tuple[CandidateChange, ...]:
    return () if value is None else (CandidateChange(name, p.get(name), value, "operations"),)


def _other_monitors(flight: FlightData, out: Result) -> set[str]:
    """A further battery monitor with a threshold whose failsafe only warns. Whether it
    watches a battery the aircraft flies on is the owner's to say, so no change."""
    p = flight.params
    flagged: set[str] = set()
    for m in MONITORS[1:]:
        if not p.get(f"{m}_MONITOR"):
            continue
        for level, thresholds, action in (
            ("low", (f"{m}_LOW_VOLT", f"{m}_LOW_MAH"), f"{m}_FS_LOW_ACT"),
            ("critical", (f"{m}_CRT_VOLT", f"{m}_CRT_MAH"), f"{m}_FS_CRT_ACT"),
        ):
            if any(p.get(t) for t in thresholds) and p.get(action) == 0:
                values = {n: p.get(n) for n in (f"{m}_MONITOR", *thresholds, action)}
                out.findings.append(
                    Finding(
                        code="params.other_battery_warn_only",
                        severity="caution",
                        confidence="high",
                        evidence=(ev(f"params:battery:{action}", "the battery monitor", values),),
                        params={"monitor": m, "param": action, "level": level},
                    )
                )
                flagged.add(action)
    return flagged


def _power_setup(flight: FlightData, rules: Mapping[str, Any], out: Result) -> set[str]:
    """The first battery monitor against the setup answers: its capacity, whether arming
    checks the pack's charge, and whether the power module was ever calibrated."""
    p, answers = flight.params, flight.answers
    flagged: set[str] = set()
    if not p.get("BATT_MONITOR"):
        return flagged
    capacity = answers.get("capacity_mah")
    current = p.get("BATT_CAPACITY")
    share = float(rules["capacity_share"])
    if (
        capacity
        and current is not None
        and abs(current - float(capacity)) > share * float(capacity)
    ):
        out.findings.append(
            Finding(
                code="params.battery_capacity",
                severity="caution",
                confidence="high",
                evidence=(
                    ev(
                        "params:battery:capacity",
                        "BATT_CAPACITY against the pack in the platform setup",
                        {"BATT_CAPACITY": current, "capacity_mah": capacity},
                    ),
                ),
                params={"value": format_value(current), "capacity": format_value(float(capacity))},
                changes=(CandidateChange("BATT_CAPACITY", current, float(capacity), "operations"),),
            )
        )
        flagged.add("BATT_CAPACITY")
    if p.get("BATT_ARM_VOLT") == 0 and p.get("BATT_ARM_MAH") == 0:
        out.findings.append(
            Finding(
                code="params.battery_arm_unchecked",
                severity="info",
                confidence="high",
                evidence=(
                    ev(
                        "params:battery:arming",
                        "the arming checks on the battery",
                        {"BATT_ARM_VOLT": 0.0, "BATT_ARM_MAH": 0.0},
                    ),
                ),
                changes=_set("BATT_ARM_VOLT", p, _starting_volts(flight, rules).get("arm")),
            )
        )
    analog = int(p.get("BATT_MONITOR", 0)) in (3, 4)  # the analog monitors have scales
    if analog and answers.get("power_module_calibrated") is False:
        names = ("BATT_VOLT_MULT", "BATT_AMP_PERVLT", "BATT_AMP_OFFSET")
        out.findings.append(
            Finding(
                code="params.power_module_uncalibrated",
                severity="caution",
                confidence="high",
                evidence=(
                    ev(
                        "params:battery:calibration",
                        "the analog battery monitor's scales, never calibrated (platform setup)",
                        {n: p.get(n) for n in names} | {"power_module_calibrated": False},
                    ),
                ),
            )
        )
    return flagged


def _battery(flight: FlightData, rules: Mapping[str, Any], out: Result) -> set[str]:
    """The first battery monitor's failsafe, and voltage settings against the cell count
    the log shows."""
    p = flight.params
    flagged: set[str] = set()
    if not p.get("BATT_MONITOR"):
        return flagged

    def add(
        code: str, severity: Any, names: list[str], params: Mapping[str, Any],
        changes: tuple[CandidateChange, ...] = (),
    ) -> None:  # fmt: skip
        values = {n: p.get(n) for n in names} | {"cells": _cells(flight)}
        out.findings.append(
            Finding(
                code=code,
                severity=severity,
                confidence="high",
                evidence=(ev(f"params:{code}:{names[0]}", "the battery parameters", values),),
                params=dict(params),
                changes=changes,
            )
        )
        flagged.update(names)

    low_set = bool(p.get("BATT_LOW_VOLT") or p.get("BATT_LOW_MAH"))
    crit_set = bool(p.get("BATT_CRT_VOLT") or p.get("BATT_CRT_MAH"))
    starting = _starting_volts(flight, rules)
    if not low_set:
        add(
            "params.battery_no_low", "caution", ["BATT_LOW_VOLT", "BATT_LOW_MAH"], {},
            _set("BATT_LOW_VOLT", p, starting.get("low")),
        )  # fmt: skip
    elif p.get("BATT_FS_LOW_ACT") == 0:
        add("params.battery_warn_only", "caution", ["BATT_FS_LOW_ACT"], {})
    if not crit_set:
        add(
            "params.battery_no_critical", "caution", ["BATT_CRT_VOLT", "BATT_CRT_MAH"], {},
            _set("BATT_CRT_VOLT", p, starting.get("critical")),
        )  # fmt: skip
    elif p.get("BATT_FS_CRT_ACT") == 0:
        add("params.battery_critical_warn_only", "out", ["BATT_FS_CRT_ACT"], {})

    cells = _cells(flight)
    if not cells:
        return flagged
    bands: Mapping[str, list[float]] = rules["cell_volts"]
    for name in ("BATT_LOW_VOLT", "BATT_CRT_VOLT"):
        value = p.get(name)
        low, high = bands["battery_threshold"]
        if value and not low <= value / cells <= high:
            add(
                "params.battery_threshold_cells", "safety", [name],
                {"param": name, "value": format_value(value),
                 "per_cell": _per_cell(value, cells), "cells": int(cells)},
            )  # fmt: skip
    volts = CELL_VOLTS.get(str(flight.answers.get("battery_chemistry") or ""))
    scaling = (("MOT_BAT_VOLT_MAX", "scaling_max", 0), ("MOT_BAT_VOLT_MIN", "scaling_min", 1))
    for name, band, index in scaling:
        value = p.get(name)
        low, high = bands[band]
        if value and not low <= value / cells <= high:
            changes: tuple[CandidateChange, ...] = ()
            if volts is not None:
                changes = (CandidateChange(name, value, round(volts[index] * cells, 2), "setup"),)
            add(
                "params.motor_scaling_cells", "caution", [name],
                {"param": name, "value": format_value(value),
                 "per_cell": _per_cell(value, cells), "cells": int(cells)},
                changes,
            )  # fmt: skip
    return flagged


def _navigation(flight: FlightData, readiness: Mapping[str, Any], out: Result) -> None:
    """GNSS, compass, estimator sources and the fence against the setup answers. Each
    needs a person's choice (the receiver's type, the sensor, the site), so they carry
    procedures rather than values."""
    p, a = flight.params, flight.answers

    def add(code: str, severity: Any, names: list[str], summary: str) -> None:
        values = {n: p.get(n) for n in names}
        out.findings.append(
            Finding(
                code=code,
                severity=severity,
                confidence="high",
                evidence=(ev(f"params:{code}", summary, values),),
            )
        )

    if int(a.get("gnss_count") or 0) >= 2 and p.get("GPS2_TYPE") == 0:
        add("params.second_gnss_unset", "caution", ["GPS2_TYPE"],
            "a second GNSS receiver in the platform setup, none in the parameters")  # fmt: skip
    if a.get("compass_mounting") == "on the flight controller" and p.get("COMPASS_MOTCT") == 0:
        add("params.compassmot_not_done", "info", ["COMPASS_MOTCT"],
            "the compass sits on the flight controller and CompassMot was never run")  # fmt: skip
    sources = [f"EK3_SRC{n}_{f}" for n in (1, 2, 3) for f in ("POSXY", "VELXY")]
    if (a.get("flies_without_gnss") is True and source_sets(p, readiness)
            and not coping_sets(p, readiness)):  # fmt: skip
        add("params.no_gnss_fallback", "caution", [n for n in sources if n in p],
            "flies without GNSS (platform setup), with no EKF source set that keeps a "
            "position without it")  # fmt: skip
    if p.get("FENCE_ENABLE") == 0:
        add("params.fence_off", "info", ["FENCE_ENABLE"], "the geofence is off")


def _round_to(value: float, places: int) -> float:
    """Mission Planner's RoundTo for negative places: to the nearest 10^-places, halves up."""
    step = 10**-places
    return float(math.floor((value + step / 2) / step) * step)


def _starting_values(flight: FlightData, rules: Mapping[str, Any], out: Result) -> None:
    """Starting values by propeller diameter for an aircraft never tuned, for the
    parameters still at the firmware's default (rules/audit.json starting_tune)."""
    r: Mapping[str, Any] = rules["starting_tune"]
    prop = flight.answers.get("prop_diameter_in")
    p, d = flight.params, flight.defaults
    if not prop or float(prop) <= 0:
        return
    prop = float(prop)
    # The filter analyzer judges the low-pass from the gyro spectrum where there is one.
    gyro = float(p.get("INS_GYRO_FILTER", 20))
    if not flight.has("GYR"):
        gyro = max(20.0, float(round(289.22 * prop**-0.838)))
    rate = (
        float(r["large_rate_filter_hz"])
        if prop >= r["large_prop_in"]
        else max(10.0, round(gyro / 2))
    )
    acc_rp = max(
        100.0,
        _round_to(-2.613267 * prop**3 + 343.39216 * prop**2 - 15083.7121 * prop + 235771, -2) / 100,
    )
    acc_y = max(80.0, _round_to(-900 * prop + 36000, -2) / 100)
    expo = min(round(0.15686 * math.log(prop) + 0.23693, 2), float(r["expo_max"]))
    wanted: dict[str, float] = {
        "INS_GYRO_FILTER": gyro, "INS_ACCEL_FILTER": 10.0,
        "ATC_RAT_RLL_FLTD": rate, "ATC_RAT_RLL_FLTT": rate,
        "ATC_RAT_PIT_FLTD": rate, "ATC_RAT_PIT_FLTT": rate,
        "ATC_RAT_YAW_FLTT": max(10.0, round(gyro / 2)),
        "ATC_ACC_P_MAX": acc_rp, "ATC_ACC_R_MAX": acc_rp, "ATC_ACC_Y_MAX": acc_y,
        "MOT_THST_EXPO": expo,
    }  # fmt: skip
    lower_only = ("ATC_ACC_P_MAX", "ATC_ACC_R_MAX", "ATC_ACC_Y_MAX")
    changes = tuple(
        CandidateChange(n, p[n], v, "setup")
        for n, v in wanted.items()
        if n in p
        and n in d
        and _is_default(n, p[n], d)  # someone's choice is kept
        and abs(p[n] - v) > TOLERANCE
        and not (n in lower_only and v > p[n])
    )
    if changes:
        out.findings.append(
            Finding(
                code="params.tuning_starting_values",
                # caution: they come first, so AutoTune waits for a flight on them
                severity="caution",
                confidence="medium",
                evidence=(
                    ev(
                        "params:starting-values",
                        f"starting values for {format_value(prop)} inch propellers",
                        {"prop_in": prop, **{c.param: c.proposed for c in changes}},
                    ),
                ),
                params={"prop": format_value(prop), "count": len(changes)},
                changes=changes,
            )
        )


def _never_tuned(flight: FlightData, rules: Mapping[str, Any], out: Result) -> None:
    names: list[str] = list(rules["never_tuned"]["names"])
    p, d = flight.params, flight.defaults
    if not all(n in p and n in d for n in names):
        return  # without recorded defaults there is no telling
    if all(_is_default(n, p[n], d) for n in names):
        _starting_values(flight, rules, out)
        out.findings.append(
            Finding(
                code="params.never_tuned",
                severity="caution",
                confidence="high",
                evidence=(
                    ev(
                        "params:never_tuned",
                        "rate gains against their defaults",
                        {n: p[n] for n in names},
                    ),
                ),
            )
        )


PARAMS_AUDIT = Analyzer(
    name="params",
    version=7,
    run=_audit,
    metrics=(),
    findings=(
        "params.out_of_range", "params.gain_out_of_range", "params.rc_failsafe_off",
        "params.fs_thr_value",
        "params.battery_warn_only", "params.battery_no_critical",
        "params.battery_critical_warn_only", "params.battery_threshold_cells",
        "params.motor_scaling_cells", "params.ekf_failsafe_report_only",
        "params.crash_check_off", "params.never_tuned", "params.unexplained",
        "params.other_battery_warn_only", "params.battery_capacity",
        "params.battery_arm_unchecked", "params.power_module_uncalibrated",
        "params.battery_no_low", "params.vibe_failsafe_off",
        "params.dead_reckoning_failsafe_off", "params.dead_reckoning_timeout_short",
        "params.ekf_failsafe_off", "params.gcs_failsafe_off", "params.arming_checks_skipped",
        "params.tuning_starting_values", "params.second_gnss_unset",
        "params.compassmot_not_done", "params.no_gnss_fallback", "params.fence_off",
        "params.undefined_bits",
    ),
)  # fmt: skip
