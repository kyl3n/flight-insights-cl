"""The filter analyzer: the notch's tracking source and the flags on an existing setup
(spec: The filter analyzer in detail, steps 1 and 2, and "What it flags on an
existing setup").

Step 2's decision tree picks the tracking source the log supports: a traditional
helicopter gets a static or RPM-sensor notch, reliable ESC RPM gets the ESC mode, an
F7 or H7 board without it gets the in-flight FFT, anything else the throttle mode.
Each flag carries the parameter changes that fix it, for the recommendation engine
to validate and order; nothing is applied.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    CandidateChange,
    Confidence,
    Evidence,
    Finding,
    Result,
    Segment,
)
from flight_insights.analyzers.motors import MOTOR_PEAK_DB, MotorPicture, picture
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.filters import NotchParams, chain
from flight_insights.filters.harmonic import ESC, FFT, FIXED, MULTI_SOURCE, RPM, RPM2, THROTTLE
from flight_insights.filters.search import Outcome, Screen, search
from flight_insights.spectral.welch import Spectrum

FFT_BOARDS = ("F7", "H7", "SITL", "Linux")  # boards ArduPilot runs the in-flight FFT on
MODE_NAMES = {
    FIXED: "fixed", THROTTLE: "throttle", RPM: "RPM sensor", ESC: "ESC telemetry",
    FFT: "in-flight FFT", RPM2: "second RPM sensor",
}  # fmt: skip
MIN_FREQ_SHARE = 0.75  # in ESC and FFT modes FREQ is the floor: this share of the hover frequency
RPM_FAIL_OPT = 1 << 5  # INS_HNTCH_OPTS: use the minimum frequency if the RPM source fails
# A harmonic is uncovered while, after the filters, it still carries power within this
# many dB of the control band's (0-40 Hz). An app rule: the low-pass lowers a harmonic
# and the floor around it alike, so standing above the floor says nothing.
UNCOVERED_BELOW_CONTROL_DB = 30.0
CONTROL_BAND_HZ = 40.0
STEADY_ENOUGH_S = 10.0
SET = "INS_HNTCH_"


def _mode(p: NotchParams) -> str:
    return MODE_NAMES.get(p.mode, f"mode {p.mode}")


def _heli(flight: FlightData) -> bool:
    frame: Mapping[str, Any] = flight.fingerprint.get("frame", {}).get("value") or {}
    return frame.get("vehicle_class") == "helicopter"


def _family(flight: FlightData) -> str | None:
    return flight.fingerprint.get("board", {}).get("family")


def _hover_throttle(flight: FlightData) -> float | None:
    value = flight.fingerprint.get("hover_throttle", {}).get("value")
    return float(value) if value else None


def _floor_freq(motor_hz: float, gyro_filter: float) -> float:
    """FREQ for a tracking notch: below the hover motor frequency, above the low-pass."""
    return float(
        max(5 * math.floor(MIN_FREQ_SHARE * motor_hz / 5), 5 * math.ceil(gyro_filter / 5) + 5)
    )


def _harmonic_mask(m: MotorPicture) -> int:
    mask = 1
    for k, p in m.harmonics.items():
        if p.prominence_db >= MOTOR_PEAK_DB:
            mask |= 1 << (k - 1)
    return mask


def _proposal(
    flight: FlightData, m: MotorPicture, current: NotchParams
) -> tuple[int, dict[str, float]] | None:
    """Step 2: the tracking source the log supports, and settings for it."""
    peak = m.motor_peak()
    motor_hz = m.fundamental_hz
    if motor_hz is None:
        return None
    gyro_filter = float(flight.params.get("INS_GYRO_FILTER", 20))
    mask = current.harmonics if current.enable and current.harmonics else _harmonic_mask(m)
    if _heli(flight):
        sensor = "RPM sensor" in (flight.fingerprint.get("rpm_sources", {}).get("value") or [])
        if sensor:
            return RPM, {"MODE": RPM, "REF": 1.0, "FREQ": _floor_freq(motor_hz, gyro_filter)}
        centre = peak.freq_hz if peak is not None else motor_hz
        return FIXED, {"MODE": FIXED, "FREQ": round(centre), "BW": round(centre / 2), "HMNCS": mask}
    if m.rpm_reliable:
        freq = _floor_freq(motor_hz, gyro_filter)
        return ESC, {
            "MODE": ESC, "REF": 1.0, "FREQ": freq, "BW": round(freq / 2), "HMNCS": mask,
            "OPTS": float(int(current.opts) | RPM_FAIL_OPT),
        }  # fmt: skip
    if _family(flight) in ("F7", "H7"):
        freq = _floor_freq(motor_hz, gyro_filter)
        return FFT, {"MODE": FFT, "REF": 1.0, "FREQ": freq, "BW": round(freq / 2), "HMNCS": mask}
    throttle = _hover_throttle(flight)
    if peak is None or throttle is None:
        return None
    return THROTTLE, {
        "MODE": THROTTLE, "FREQ": round(peak.freq_hz), "REF": round(throttle, 3),
        "BW": round(peak.freq_hz / 2), "HMNCS": mask,
    }  # fmt: skip


def _changes(
    current: NotchParams, settings: Mapping[str, float], prefix: str = SET
) -> tuple[CandidateChange, ...]:
    values = {
        "ENABLE": float(current.enable), "MODE": float(current.mode), "FREQ": current.freq,
        "BW": current.bw, "REF": current.ref, "HMNCS": float(current.harmonics),
        "OPTS": float(current.opts),
    }  # fmt: skip
    out: list[CandidateChange] = []
    for name, value in settings.items():
        if values.get(name) != float(value):
            out.append(CandidateChange(prefix + name, values.get(name), float(value), "filters"))
    return tuple(out)


def _centre(flight: FlightData, m: MotorPicture, p: NotchParams) -> float | None:
    """Where the first notch of the set sat in hover: the logged centre when the log
    holds it, else the model's."""
    for name, column in (("FCN", "CF1"), ("FCNS", "CF")):
        t = flight.table(name)
        if t is None or column not in t.column_names:
            continue
        times = np.asarray(t.column("TimeUS").to_numpy(), np.int64)
        inst = np.asarray(t.column("I").to_numpy(), np.int64)
        sel = inst == 0
        inside = np.zeros(len(times), bool)
        for a, b in m.spans:
            inside |= (times >= a) & (times <= b)
        values = np.asarray(t.column(column).to_numpy(zero_copy_only=False), np.float64)
        values = values[sel & inside]
        values = values[np.isfinite(values) & (values > 0)]
        if len(values):
            return float(np.median(values))
    if m.spectrum is None:
        return None
    tables = {n: t for n in ("ESC", "MOTB", "RPM") if (t := flight.table(n)) is not None}
    first, last = m.spans[0][0], m.spans[-1][1]
    c = chain(flight.params, m.spectrum.rate_hz, _family(flight), tables, first, last)
    if not c.sets or c.sets[0].notch.p.prefix != SET:
        return None
    notches = c.sets[0].notch.notches(c.typical_sources(first, last)[0])
    return notches[0].center_hz if notches else None


def _post_filter(flight: FlightData, m: MotorPicture) -> Spectrum | None:
    """The hover spectrum through the current filters (transfer-function method)."""
    if m.spectrum is None:
        return None
    tables = {
        n: t for n in ("ESC", "MOTB", "RPM", "FCN", "FCNS") if (t := flight.table(n)) is not None
    }
    first, last = m.spans[0][0], m.spans[-1][1]
    c = chain(flight.params, m.spectrum.rate_hz, _family(flight), tables, first, last)
    gain = c.power_gain(m.spectrum.freqs, first, last)
    s = m.spectrum
    return Spectrum(s.freqs, s.psd * gain[None, :], s.rate_hz, s.windows, s.seconds)


def _filters(flight: FlightData, reg: Registry) -> Result:
    m = picture(flight)
    if m.spectrum is None:
        return Result(skipped="no pre-filter gyro data: the logging analyzer says how to get it")
    p1 = NotchParams.read(flight.params, SET)
    p2 = NotchParams.read(flight.params, "INS_HNTC2_")
    enabled = p1.enable and p1.num_harmonics > 0
    confident = m.kind == "hover" and m.seconds >= STEADY_ENOUGH_S
    confidence: Confidence = "high" if confident else "medium"
    picture_ev = Evidence(
        id="filters:motors",
        kind="series",
        summary=(
            f"{m.kind} spectrum of gyro {m.instance} over {m.seconds:.1f} s: motors at "
            f"{m.fundamental_hz or 0:.1f} Hz ({m.fundamental_from}), harmonics with energy "
            f"{sorted(m.harmonics)}; ESC RPM "
            + ("reliable" if m.rpm_reliable else "not usable: " + "; ".join(m.rpm_problems))
        ),
        messages=("GYR", "ESC"),
        fields=("GyrX", "GyrY", "GyrZ", "RPM", "Err"),
        segment=Segment(m.kind, m.spans[0][0], m.spans[-1][1]),
        values={
            "fundamental_hz": m.fundamental_hz,
            "fundamental_from": m.fundamental_from,
            "esc_hz": m.esc_hz,
            "harmonics": {
                str(k): {"hz": round(p.freq_hz, 2), "prominence_db": round(p.prominence_db, 1)}
                for k, p in sorted(m.harmonics.items())
            },
            "rpm_reliable": m.rpm_reliable,
            "rpm_problems": m.rpm_problems,
            "seconds": round(m.seconds, 1),
        },
    )
    setup: dict[str, Any] = {
        f"{p.prefix}{name}": value
        for p in (p1, p2)
        for name, value in (
            ("ENABLE", int(p.enable)), ("MODE", p.mode), ("FREQ", p.freq), ("BW", p.bw),
            ("REF", p.ref), ("HMNCS", p.harmonics), ("OPTS", p.opts), ("FM_RAT", p.fm_rat),
        )
    }  # fmt: skip
    setup["INS_GYRO_FILTER"] = flight.params.get("INS_GYRO_FILTER")
    setup["board_family"] = _family(flight)
    setup_ev = Evidence(
        id="filters:setup",
        kind="parameter",
        summary=(
            f"harmonic notch {_mode(p1)} at FREQ {p1.freq:g} Hz, BW {p1.bw:g} Hz, REF {p1.ref:g}"
            if enabled
            else "harmonic notch off"
        ),
        fields=tuple(k for k in setup if k.startswith("INS_")),
        values=setup,
    )
    out = Result()
    proposal = _proposal(flight, m, p1)

    if not enabled and not (p2.enable and p2.num_harmonics) and m.clear_motor_noise():
        peak = m.motor_peak()
        changes: tuple[CandidateChange, ...] = ()
        if proposal is not None:
            changes = _changes(p1, {"ENABLE": 1.0, **proposal[1]})
        out.findings.append(
            Finding(
                code="filter.no_notch",
                severity="caution",
                confidence=confidence,
                evidence=(picture_ev, setup_ev),
                params={
                    "peak_hz": round(peak.freq_hz if peak else m.fundamental_hz or 0, 1),
                    "prominence_db": round(peak.prominence_db if peak else 0, 1),
                    "mode": MODE_NAMES.get(proposal[0], "") if proposal else "",
                },
                changes=changes,
            )
        )
        return _with_search(flight, reg, m, p1, proposal, out, picture_ev, setup_ev, confidence)
    if not enabled:
        return out

    switching = False
    if _heli(flight) and p1.mode == THROTTLE:
        changes = _changes(p1, proposal[1]) if proposal else ()
        out.findings.append(
            Finding(
                code="filter.heli_throttle",
                severity="out",
                confidence="high",
                evidence=(setup_ev,),
                params={},
                changes=changes,
            )
        )
        switching = True
    elif proposal is not None and proposal[0] == ESC and p1.mode in (FIXED, THROTTLE, FFT):
        out.findings.append(
            Finding(
                code="filter.better_source",
                severity="caution",
                confidence=confidence,
                evidence=(picture_ev, setup_ev),
                params={"current": _mode(p1), "better": MODE_NAMES[ESC]},
                changes=_changes(p1, proposal[1]),
            )
        )
        switching = True
    if p1.mode == FFT and _family(flight) not in FFT_BOARDS:
        changes = _changes(p1, proposal[1]) if proposal and not switching else ()
        out.findings.append(
            Finding(
                code="filter.fft_weak_board",
                severity="out",
                confidence="high",
                evidence=(setup_ev,),
                params={"board": _family(flight) or "this board"},
                changes=changes,
            )
        )
        switching = True

    peak = m.motor_peak()
    centre = _centre(flight, m, p1)
    if peak is not None and centre is not None and p1.harmonics & 1:
        offset = abs(centre - peak.freq_hz)
        if offset > p1.bw / 4:
            changes = ()
            if not switching and p1.mode in (FIXED, THROTTLE):
                settings: dict[str, float] = {"FREQ": float(round(peak.freq_hz))}
                throttle = _hover_throttle(flight)
                if p1.mode == THROTTLE and throttle:
                    settings["REF"] = round(throttle, 3)
                changes = _changes(p1, settings)
            out.findings.append(
                Finding(
                    code="filter.not_tracking",
                    severity="out" if offset > p1.bw / 2 else "caution",
                    confidence=confidence,
                    evidence=(picture_ev, setup_ev),
                    params={
                        "centre_hz": round(centre, 1),
                        "peak_hz": round(peak.freq_hz, 1),
                        "offset_hz": round(offset, 1),
                    },
                    changes=changes,
                )
            )

    if p1.mode == ESC and m.esc_hz:
        floor = p1.freq * p1.fm_rat
        if floor > 0.95 * m.esc_hz:
            freq = _floor_freq(m.esc_hz, float(flight.params.get("INS_GYRO_FILTER", 20)))
            out.findings.append(
                Finding(
                    code="filter.freq_above_hover",
                    severity="out",
                    confidence=confidence,
                    evidence=(picture_ev, setup_ev),
                    params={"freq": round(floor, 1), "motor_hz": round(m.esc_hz, 1)},
                    changes=_changes(p1, {"FREQ": freq}),
                )
            )

    post = _post_filter(flight, m)
    if post is not None and m.fundamental_hz:
        control = float(post.band_power(0.0, CONTROL_BAND_HZ).sum())
        half = 3 * post.resolution_hz
        for k, p in sorted(m.harmonics.items()):
            if k < 2 or p1.harmonics >> (k - 1) & 1 or p.prominence_db < MOTOR_PEAK_DB:
                continue
            left = float(post.band_power(p.freq_hz - half, p.freq_hz + half).sum())
            if left <= 0 or 10 * np.log10(left / max(control, 1e-30)) < -UNCOVERED_BELOW_CONTROL_DB:
                continue
            out.findings.append(
                Finding(
                    code="filter.harmonic_uncovered",
                    severity="caution",
                    confidence=confidence,
                    evidence=(picture_ev, setup_ev),
                    params={"harmonic": k, "hz": round(p.freq_hz, 1)},
                    changes=()
                    if switching
                    else _changes(p1, {"HMNCS": float(p1.harmonics | 1 << (k - 1))}),
                )
            )

    single = not (p1.opts & MULTI_SOURCE)
    widest = p1.freq if single else p1.freq / 2
    if p1.bw > widest:
        bw = round(p1.freq / 2) if single else round(p1.freq / 4)
        out.findings.append(
            Finding(
                code="filter.bandwidth_wide",
                severity="caution",
                confidence="high",
                evidence=(setup_ev,),
                params={"bw": p1.bw, "freq": p1.freq},
                changes=() if switching else _changes(p1, {"BW": float(bw)}),
            )
        )
    return _with_search(flight, reg, m, p1, proposal, out, picture_ev, setup_ev, confidence)


def _cpu_load_p95(flight: FlightData) -> float | None:
    pm = flight.table("PM")
    if pm is None or "Load" not in pm.column_names:
        return None
    t = np.asarray(pm.column("TimeUS").to_numpy(), np.int64)
    load = si.values(pm, "Load")[(t >= flight.arm_us) & (t <= flight.disarm_us)]
    return float(np.percentile(load, 95)) if len(load) else None


def _with_search(
    flight: FlightData,
    reg: Registry,
    m: MotorPicture,
    p1: NotchParams,
    proposal: tuple[int, dict[str, float]] | None,
    out: Result,
    picture_ev: Evidence,
    setup_ev: Evidence,
    confidence: Confidence,
) -> Result:
    """Steps 3 to 5: search the settings around the chosen source. A better set found
    carries the whole change; the flags above then explain it, without their own."""
    if m.spectrum is None:
        return out
    enabled = p1.enable and p1.num_harmonics > 0
    if proposal is not None and (not enabled or proposal[0] != p1.mode or _switching(out)):
        base: dict[str, float] = {
            k: v for k, v in proposal[1].items() if k in ("MODE", "REF", "FREQ", "OPTS")
        }
    elif enabled:
        base = {"MODE": float(p1.mode), "REF": p1.ref, "FREQ": p1.freq, "OPTS": float(p1.opts)}
    else:
        return out
    tables = {
        n: t for n in ("ESC", "MOTB", "RPM", "FCN", "FCNS") if (t := flight.table(n)) is not None
    }
    imus = int(flight.fingerprint.get("imu_count", {}).get("value") or 1)
    screen = Screen(
        m.spectrum, flight.params, tables, m.spans, _family(flight), imus, reg.rules["filters"]
    )
    outcome = search(screen, base, _harmonic_mask(m), max(len(m.escs), 1), _cpu_load_p95(flight))
    ev = _search_evidence(outcome, m)
    out.evidence.append(ev)
    if outcome.best is None:
        return out
    best, now = outcome.best, outcome.current
    changes = _changes(
        p1, {k: v for k, v in best.setting.notch.items() if k not in ("ATT", "FM_RAT")}
    )
    gyro_filter = float(flight.params.get("INS_GYRO_FILTER", 0))
    if best.setting.gyro_filter != gyro_filter:
        changes += (
            CandidateChange("INS_GYRO_FILTER", gyro_filter, best.setting.gyro_filter, "filters"),
        )
    explained = [dataclasses.replace(f, changes=()) for f in out.findings]
    better = Finding(
        code="filter.better_settings",
        severity="caution" if now.noise_db - best.noise_db >= 6 else "info",
        confidence=confidence,
        evidence=(picture_ev, setup_ev, ev),
        params={
            "lag_now": round(now.lag_deg, 1),
            "lag_new": round(best.lag_deg, 1),
            "lag_hz": int(screen.lag_hz),
            "noise_change_db": round(best.noise_db - now.noise_db, 1),
            "notches_now": now.notches,
            "notches_new": best.notches,
            # the flags this whole set answers, their own changes replaced by it
            "explains": [f.code for f in out.findings],
        },
        changes=changes,
    )
    return Result(findings=[*explained, better], metrics=out.metrics, evidence=out.evidence)


def _switching(out: Result) -> bool:
    return any(
        f.code in ("filter.better_source", "filter.heli_throttle", "filter.fft_weak_board")
        for f in out.findings
    )


def _search_evidence(outcome: Outcome, m: MotorPicture) -> Evidence:
    def row(s: Any) -> dict[str, Any]:
        return {
            "notch": dict(s.setting.notch),
            "INS_GYRO_FILTER": s.setting.gyro_filter,
            "noise_db": round(s.noise_db, 2),
            "lag_deg": round(s.lag_deg, 1),
            "notches": s.notches,
        }

    return Evidence(
        id="filters:search",
        kind="series",
        summary=(
            f"{outcome.candidates} settings screened on the {m.kind} spectrum, {outcome.feasible} "
            f"within the noise target of {outcome.target_db:.1f} dB"
            + ("" if outcome.best is not None else "; none better than now by enough")
        ),
        messages=("GYR",),
        segment=Segment(m.kind, m.spans[0][0], m.spans[-1][1]),
        values={
            "current": row(outcome.current),
            "best": row(outcome.best) if outcome.best is not None else None,
            "target_db": round(outcome.target_db, 2),
            "screened": outcome.candidates,
            "feasible": outcome.feasible,
        },
    )


FILTERS = Analyzer(
    name="filters",
    version=3,
    run=_filters,
    metrics=(),
    findings=(
        "filter.no_notch",
        "filter.heli_throttle",
        "filter.better_source",
        "filter.fft_weak_board",
        "filter.not_tracking",
        "filter.freq_above_hover",
        "filter.harmonic_uncovered",
        "filter.bandwidth_wide",
        "filter.better_settings",
    ),
)
