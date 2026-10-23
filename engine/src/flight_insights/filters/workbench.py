"""The filter workbench's data (spec: User interface, "Filter workbench"; mock-up
Workbench): what the gyro saw before the filters, what the filters let through as
flown, and what a candidate setting would let through, with the noise, phase lag and
notch count each costs, plus a spectrogram of the whole flight with the motor
frequency and the notch centre over it.

The spectra are the analysers' own: the hover spectrum the filter search screens
candidates on, and the sample-by-sample model of the flight's own filters. Only the
spectrogram is pooled for display (spec: Plotting at log scale); every number the
workbench reports comes from full-rate data.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.motors import MotorPicture, picture
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.filters.check import Check, check
from flight_insights.filters.harmonic import NotchParams
from flight_insights.filters.search import SET, Screen
from flight_insights.spectral.gyro import runs
from flight_insights.spectral.welch import windows

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]

TABLES = ("GYR", "ESC", "MOTB", "FCN", "FCNS", "RPM")
# What a person may try in the workbench: the first harmonic notch and the low-pass.
EDITABLE = (
    "INS_HNTCH_ENABLE", "INS_HNTCH_MODE", "INS_HNTCH_FREQ", "INS_HNTCH_BW", "INS_HNTCH_REF",
    "INS_HNTCH_HMNCS", "INS_HNTCH_OPTS", "INS_HNTCH_ATT", "INS_GYRO_FILTER",
)  # fmt: skip
SPECTROGRAM_HZ = 4.0  # window resolution: finer in time than the 1 Hz analysis spectra
MAX_COLUMNS = 240  # display only
MAX_ROWS = 256
FLOOR = 1e-12


@dataclass
class Bench:
    flight: FlightData
    motors: MotorPicture
    screen: Screen
    model: Check | None  # the flight's own filters, sample by sample
    spectrogram: dict[str, Any] | None
    cap: int | None  # notches the board can run


def _db(psd: Floats) -> list[float]:
    return [round(float(v), 2) for v in 10 * np.log10(np.maximum(psd, FLOOR))]


def _pool(a: Floats, size: int, axis: int) -> Floats:
    """Averages neighbours along `axis` until it is at most `size` long."""
    n = a.shape[axis]
    k = int(np.ceil(n / size))
    if k <= 1:
        return a
    cut = n - n % k
    trimmed = np.take(a, np.arange(cut), axis=axis)
    shape = list(trimmed.shape)
    shape[axis : axis + 1] = [cut // k, k]
    return np.asarray(trimmed.reshape(shape).mean(axis=axis + 1), np.float64)


def _spectrogram(flight: FlightData, instance: int) -> dict[str, Any] | None:
    gyr = flight.table("GYR")
    if gyr is None:
        return None
    w = windows(runs(gyr, instance, flight.arm_us, flight.disarm_us), SPECTROGRAM_HZ)
    if w is None:
        return None
    power = w.psd.sum(axis=1)  # every axis: all the noise the filters face
    times = (w.start_us + w.end_us) / 2
    order = np.argsort(times)
    power, times = power[order], times[order]
    pooled = _pool(_pool(power, MAX_COLUMNS, 0), MAX_ROWS, 1)
    t_us = _pool(np.asarray(times, np.float64)[:, None], MAX_COLUMNS, 0)[:, 0]
    freqs = _pool(np.asarray(w.freqs, np.float64)[None, :], MAX_ROWS, 1)[0]
    out: dict[str, Any] = {
        "t_s": [round(float((t - flight.arm_us) / 1e6), 2) for t in t_us],
        "freqs": [round(float(f), 2) for f in freqs],
        "db": [_db(row) for row in pooled],
    }
    edges = np.concatenate([[flight.arm_us], (t_us[1:] + t_us[:-1]) / 2, [flight.disarm_us]])
    esc = flight.table("ESC")
    if esc is not None and "RPM" in esc.column_names:
        t = np.asarray(esc.column("TimeUS").to_numpy(), np.int64)
        hz = si.values(esc, "RPM") / 60.0
        out["motor_hz"] = _binned(t, hz, edges)
    fcn = flight.table("FCN")
    if fcn is not None and "CF1" in fcn.column_names:
        t = np.asarray(fcn.column("TimeUS").to_numpy(), np.int64)
        out["notch_hz"] = _binned(t, si.values(fcn, "CF1"), edges)
    return out


def _binned(t: Any, v: Any, edges: Floats) -> list[float | None]:
    """The median of the values in each column, None where there are none."""
    out: list[float | None] = []
    for a, b in itertools.pairwise(edges):
        sel = (t >= a) & (t < b) & (v > 0)
        out.append(round(float(np.median(v[sel])), 1) if sel.any() else None)
    return out


def load(flight: FlightData, reg: Registry) -> Bench | None:
    """Everything the workbench needs once per flight; None without pre-filter gyro data."""
    m = picture(flight)
    if m.spectrum is None or m.instance is None or not m.spans:
        return None
    tables = {n: t for n in TABLES if (t := flight.table(n)) is not None}
    imus = int(flight.fingerprint.get("imu_count", {}).get("value") or 1)
    family = flight.fingerprint.get("board", {}).get("family")
    rules: Mapping[str, Any] = reg.rules["filters"]
    screen = Screen(m.spectrum, flight.params, tables, m.spans, family, imus, rules)
    model = check(flight.params, tables, m.spans, imus, family)
    cap = rules["max_notches"].get(family) if family else None
    return Bench(flight, m, screen, model, _spectrogram(flight, m.instance), cap)


def _centres(bench: Bench, p: NotchParams) -> list[float]:
    """Where the set's notches sit in hover: the model's first centre for the flight's
    own setting, the motor fundamental for another mode, times each harmonic."""
    if not p.enable or not p.num_harmonics:
        return []
    base: float | None = None
    own = NotchParams.read(bench.flight.params, SET)
    if bench.model is not None and p.mode == own.mode and bench.model.tracking:
        base = bench.model.tracking[0].get("model_centre_hz")
    if base is None:
        base = bench.motors.fundamental_hz or p.freq
    return [round(float(base) * (k + 1), 1) for k in range(16) if p.harmonics >> k & 1]


def evaluate(bench: Bench, params: Mapping[str, float]) -> dict[str, Any]:
    """What the filters with `params` would let through and cost, on this flight."""
    s = bench.screen
    p = NotchParams.read(params, SET)
    part = s.notch_part(p)
    if part is None:
        return {"available": False, "reason": "the log lacks this tracking source"}
    lp = s.lowpass_part(float(params.get("INS_GYRO_FILTER", 0)))
    gain = part[0] * s.other_gain * lp[0]
    df = s.spectrum.resolution_hz
    noise = float(np.sum(s.pre[s.band] * gain[s.band]) * df)
    notches = part[2] + s.other_count
    return {
        "available": True,
        "noise_db": round(10 * float(np.log10(max(noise, 1e-30))), 2),
        "lag_deg": round(part[1] + s.other_lag + lp[1], 2),
        "notches": notches,
        "within_budget": bench.cap is None or notches <= bench.cap,
        "centres_hz": _centres(bench, p),
        "bandwidth_hz": p.bw if p.enable else None,
        "lowpass_hz": float(params.get("INS_GYRO_FILTER", 0)),
        "spectrum_db": [_db(axis * gain) for axis in s.spectrum.psd],
    }


def payload(bench: Bench, candidate: Mapping[str, float] | None = None) -> dict[str, Any]:
    """The workbench view: spectra per axis before, as flown (logged, else modelled)
    and predicted, with the candidate's numbers beside the flight's own."""
    s = bench.screen
    flown: list[list[float]] | None = None
    flown_from = None
    pred = bench.model.prediction if bench.model is not None else None
    if pred is not None and len(pred.freqs) == len(s.spectrum.freqs):
        source = pred.logged if pred.logged is not None else pred.predicted
        flown, flown_from = (
            [_db(a) for a in source],
            "logged" if pred.logged is not None else "model",
        )
    params = dict(bench.flight.params)
    out: dict[str, Any] = {
        "segment": bench.motors.kind,
        "spans_s": [
            [round((a - bench.flight.arm_us) / 1e6, 1), round((b - bench.flight.arm_us) / 1e6, 1)]
            for a, b in bench.motors.spans
        ],
        "seconds": round(s.spectrum.seconds, 1),
        "rate_hz": round(s.spectrum.rate_hz, 1),
        "instance": bench.motors.instance,
        "noise_band_hz": [float(s.rules["noise_band_low_hz"]), round(0.45 * s.spectrum.rate_hz, 1)],
        "lag_hz": s.lag_hz,
        "notch_cap": bench.cap,
        "tracking": bench.model.tracking if bench.model is not None else [],
        "rpm_reliable": bench.motors.rpm_reliable,
        "freqs": [round(float(f), 2) for f in s.spectrum.freqs],
        "before_db": [_db(a) for a in s.spectrum.psd],
        "flown_db": flown,
        "flown_from": flown_from,
        "params": {k: params[k] for k in EDITABLE if k in params},
        "current": evaluate(bench, params),
        "spectrogram": bench.spectrogram,
    }
    if candidate:
        out["candidate"] = evaluate(bench, params | dict(candidate))
        out["candidate_params"] = dict(candidate)
    return out
