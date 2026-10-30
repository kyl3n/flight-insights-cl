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
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.motors import MOTOR_PEAK_DB, MotorPicture, picture
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.filters import NotchParams, chain
from flight_insights.filters.harmonic import (
    ESC,
    FFT,
    FIXED,
    MULTI_SOURCE,
    NYQUIST_CUTOFF,
    RPM,
    RPM2,
    SETS,
    THROTTLE,
)
from flight_insights.filters.search import Outcome, Screen, search
from flight_insights.fingerprint.devices import fast_sampling_capable
from flight_insights.spectral.peaks import peaks
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
SET = SETS[0]


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


FFT_DEFAULTS = {"FFT_MINHZ": 50.0, "FFT_MAXHZ": 450.0}  # AP_GyroFFT.cpp var_info, 4.7.0
FFT_MAX_SHARE = 1.5  # FFT_MAXHZ: this share of the hover motor frequency, as motors spin up
FFT_MAX_CAP_HZ = 480.0  # AP_GyroFFT caps MAXHZ at 0.48 of its sample rate; 1 kHz is the slowest
# A peak further than this share of the motor frequency from every motor harmonic is not
# the motors': a frame or payload resonance, which a static notch on a free set covers.
NON_MOTOR_SHARE = 0.15
ALL_IMUS_OPT = 1 << 3  # INS_HNTCH_OPTS: EnableOnAllIMUs
# INS_GYRO_RATE: 1, 2, 4 or 8 kHz. The parameter's guidance: above 1 kHz on F7 and H7 only,
# above 2 kHz only with careful tuning, so the app proposes 2 kHz at most.
GYRO_RATE_MAX_INDEX = 1


def _fft_changes(
    flight: FlightData, m: MotorPicture, changes: tuple[CandidateChange, ...]
) -> tuple[CandidateChange, ...]:
    """A notch moved onto the in-flight FFT needs the FFT on, searching a band that
    holds the motors: from the notch's own minimum up past the motors' frequency in a
    climb. FFT_MINHZ's default (50 Hz) is above a large aircraft's notch floor."""
    mode = next((c.proposed for c in changes if c.param == f"{SET}MODE"), None)
    if mode != FFT or not m.fundamental_hz:
        return changes
    p = flight.params
    out = list(changes)
    if not int(p.get("FFT_ENABLE", 0)):
        out.append(CandidateChange("FFT_ENABLE", p.get("FFT_ENABLE"), 1.0, "filters"))
    floor = next((c.proposed for c in changes if c.param == f"{SET}FREQ"), None)
    low = float(p.get("FFT_MINHZ", FFT_DEFAULTS["FFT_MINHZ"]))
    if floor is not None and floor < low:
        minimum = float(max(20, math.floor(floor)))
        out.append(CandidateChange("FFT_MINHZ", p.get("FFT_MINHZ"), minimum, "filters"))
    high = float(p.get("FFT_MAXHZ", FFT_DEFAULTS["FFT_MAXHZ"]))
    needed = min(FFT_MAX_CAP_HZ, float(math.ceil(FFT_MAX_SHARE * m.fundamental_hz / 10) * 10))
    if needed > high:
        out.append(CandidateChange("FFT_MAXHZ", p.get("FFT_MAXHZ"), needed, "filters"))
    return tuple(out)


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
        n: t
        for n in ("ESC", "MOTB", "RPM", "FCN", "FCNS", "FTN", "FTNS")
        if (t := flight.table(n)) is not None
    }
    first, last = m.spans[0][0], m.spans[-1][1]
    c = chain(flight.params, m.spectrum.rate_hz, _family(flight), tables, first, last)
    gain = c.power_gain(m.spectrum.freqs, first, last)
    s = m.spectrum
    return Spectrum(s.freqs, s.psd * gain[None, :], s.rate_hz, s.windows, s.seconds)


def _above_nyquist(
    flight: FlightData,
    m: MotorPicture,
    p1: NotchParams,
    keep_mask: bool,
    busy_cpu_pct: float,
    setup_ev: Evidence,
    picture_ev: Evidence,
) -> list[Finding]:
    """Harmonics the notch is set to cover whose hover frequency is at or above 0.48 of
    the gyro rate: the firmware drops those notches (HarmonicNotchFilter.cpp,
    HARMONIC_NYQUIST_CUTOFF). On an F7 or H7 with room, a faster gyro rate brings them
    back when its gyro can sample fast; elsewhere they come out of the mask, which then
    says what the notch really does."""
    if m.spectrum is None or not m.fundamental_hz:
        return []
    limit = m.spectrum.rate_hz * NYQUIST_CUTOFF
    bits = [k for k in range(1, 17) if p1.harmonics >> (k - 1) & 1]
    dropped = [k for k in bits if k * m.fundamental_hz >= limit]
    if not dropped:
        return []
    p = flight.params
    highest = max(dropped) * m.fundamental_hz
    changes: tuple[CandidateChange, ...] = ()
    cpu = _cpu_load_p95(flight)
    room = cpu is None or cpu < busy_cpu_pct
    instance = m.instance or 0
    faster = _family(flight) in ("F7", "H7") and room and fast_sampling_capable(p, instance) is True
    if faster:
        index = next(
            (i for i in range(GYRO_RATE_MAX_INDEX + 1) if 1000 * 2**i * NYQUIST_CUTOFF > highest),
            GYRO_RATE_MAX_INDEX,
        )
        current = p.get("INS_GYRO_RATE")
        if current is None or int(current) < index:
            changes += (CandidateChange("INS_GYRO_RATE", current, float(index), "filters"),)
        bit = 1 << instance
        mask = p.get("INS_FAST_SAMPLE")
        if mask is None or not int(mask) & bit:
            fast = float(int(mask or 0) | bit)
            changes += (CandidateChange("INS_FAST_SAMPLE", mask, fast, "filters"),)
    if not changes and not keep_mask:
        kept = p1.harmonics & ~sum(1 << (k - 1) for k in dropped)
        if kept:
            changes = _changes(p1, {"HMNCS": float(kept)})
    return [
        Finding(
            code="filter.harmonics_above_nyquist",
            severity="caution" if faster else "info",
            confidence="high",
            evidence=(picture_ev, setup_ev),
            params={
                "harmonics": dropped,
                "hz": round(highest, 1),
                "limit_hz": round(limit, 1),
                "gyro_hz": round(m.spectrum.rate_hz),
            },
            changes=changes,
        )
    ]


def _unnotched(
    flight: FlightData,
    m: MotorPicture,
    p1: NotchParams,
    confidence: Confidence,
    picture_ev: Evidence,
    setup_ev: Evidence,
) -> list[Finding]:
    """A strong peak above the control band that is no motor harmonic and still shows
    after the filters: a frame or payload resonance. It gets a static notch on the first
    harmonic notch set that is off, or no change when the board has none free."""
    post = _post_filter(flight, m)
    if post is None or not m.fundamental_hz or m.kind != "hover":
        return []  # over a whole flight the motors sweep too far to tell a resonance apart
    f0 = m.fundamental_hz
    spinning = [q.median_hz for q in m.escs if np.isfinite(q.median_hz)] or [f0]
    low, high = min(spinning), max(spinning)
    tol = NON_MOTOR_SHARE * f0

    def motors(hz: float) -> bool:
        return any(
            k * low - tol <= hz <= k * high + tol for k in range(1, int(hz / max(low, 1.0)) + 2)
        )

    control = float(post.band_power(0.0, CONTROL_BAND_HZ).sum())
    half = 3 * post.resolution_hz
    found = None
    for pk in peaks(post, CONTROL_BAND_HZ, post.rate_hz / 2, MOTOR_PEAK_DB):
        if motors(pk.freq_hz):
            continue
        left = float(post.band_power(pk.freq_hz - half, pk.freq_hz + half).sum())
        if left > 0 and 10 * np.log10(left / max(control, 1e-30)) >= -UNCOVERED_BELOW_CONTROL_DB:
            found = pk
            break
    if found is None:
        return []
    hz = float(round(found.freq_hz))
    free = next(
        (
            s
            for s in SETS[1:]
            if f"{s}ENABLE" in flight.params and not int(flight.params[f"{s}ENABLE"])
        ),
        None,
    )
    changes: tuple[CandidateChange, ...] = ()
    if free is not None:
        settings = {
            "ENABLE": 1.0, "MODE": float(FIXED), "FREQ": hz, "BW": float(round(hz / 4)),
            "HMNCS": 1.0, "OPTS": float(p1.opts & ALL_IMUS_OPT),
        }  # fmt: skip
        # a set that is off hides its other parameters, so they have no value to show
        changes = tuple(
            CandidateChange(free + k, flight.params.get(free + k), v, "filters")
            for k, v in settings.items()
            if flight.params.get(free + k) != v
        )
    ev = Evidence(
        id="filters:unnotched",
        kind="series",
        summary=(
            f"after the filters, a peak at {found.freq_hz:.1f} Hz, {found.prominence_db:.1f} dB "
            f"above the floor, more than {NON_MOTOR_SHARE:.0%} of the motor frequency from "
            f"every harmonic of the motors at {low:.1f} to {high:.1f} Hz"
        ),
        messages=("GYR",),
        fields=("GyrX", "GyrY", "GyrZ"),
        segment=Segment(m.kind, m.spans[0][0], m.spans[-1][1]),
        values={
            "peak_hz": round(found.freq_hz, 1),
            "prominence_db": round(found.prominence_db, 1),
            "motor_hz": [round(low, 1), round(high, 1)],
            "free_set": free,
        },
    )
    return [
        Finding(
            code="filter.peak_unnotched",
            severity="caution",
            confidence=confidence if m.fundamental_from == "ESC RPM" else "medium",
            evidence=(picture_ev, setup_ev, ev),
            params={"hz": hz, "set": (free or "").rstrip("_")},
            changes=changes,
        )
    ]


def _filters(flight: FlightData, reg: Registry) -> Result:
    m = picture(flight)
    if m.spectrum is None:
        return Result(skipped="no pre-filter gyro data: the logging analyzer says how to get it")
    res = _notch(flight, reg, m)
    if m.kind == "hover" and m.fundamental_hz:
        # the motors' frequency in hover, which the speed build-up carries to its step
        res.evidence.append(_picture_evidence(m))
        res.metrics.append(MetricValue("filter.motor_hz", "hover", round(m.fundamental_hz, 1),
                                       None, "filters:motors"))  # fmt: skip
    return res


def _picture_evidence(m: MotorPicture) -> Evidence:
    return Evidence(
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


def _notch(flight: FlightData, reg: Registry, m: MotorPicture) -> Result:
    p1 = NotchParams.read(flight.params, SET)
    # the other sets this board has (a set it lacks has no parameters in the log)
    others = [NotchParams.read(flight.params, s) for s in SETS[1:] if f"{s}ENABLE" in flight.params]
    enabled = p1.enable and p1.num_harmonics > 0
    confident = m.kind == "hover" and m.seconds >= STEADY_ENOUGH_S
    confidence: Confidence = "high" if confident else "medium"
    picture_ev = _picture_evidence(m)
    setup: dict[str, Any] = {
        f"{p.prefix}{name}": value
        for p in (p1, *others)
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

    others_on = any(p.enable and p.num_harmonics for p in others)
    if not enabled and not others_on and m.clear_motor_noise():
        peak = m.motor_peak()
        changes: tuple[CandidateChange, ...] = ()
        if proposal is not None:
            changes = _fft_changes(flight, m, _changes(p1, {"ENABLE": 1.0, **proposal[1]}))
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
        res = _with_search(flight, reg, m, p1, proposal, out, picture_ev, setup_ev, confidence)
        res.findings.extend(_unnotched(flight, m, p1, confidence, picture_ev, setup_ev))
        return res
    if not enabled:
        out.findings.extend(_unnotched(flight, m, p1, confidence, picture_ev, setup_ev))
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
    res = _with_search(flight, reg, m, p1, proposal, out, picture_ev, setup_ev, confidence)
    # after the search, which takes the changes of the flags before it into its own set;
    # a mask it proposes already leaves out what the gyro cannot carry
    busy = float(reg.rules["filters"]["busy_cpu_pct"])
    searched = switching or any(f.code == "filter.better_settings" for f in res.findings)
    res.findings.extend(_above_nyquist(flight, m, p1, searched, busy, setup_ev, picture_ev))
    res.findings.extend(_unnotched(flight, m, p1, confidence, picture_ev, setup_ev))
    return res


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
        n: t
        for n in ("ESC", "MOTB", "RPM", "FCN", "FCNS", "FTN", "FTNS")
        if (t := flight.table(n)) is not None
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
    changes = _fft_changes(flight, m, changes)
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
    version=6,
    run=_filters,
    metrics=("filter.motor_hz",),
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
        "filter.peak_unnotched",
        "filter.harmonics_above_nyquist",
    ),
)
