"""Search for better gyro filter settings (spec: The filter analyzer in detail, steps 3
to 5).

Candidates vary the harmonic notch's harmonics, single, double or triple notches,
one notch per motor, its bandwidth, and the gyro low-pass cutoff, for the tracking
source the analyzer chose. Each is screened by the transfer-function method on the
flight's own hover spectrum: the noise it would let through above the control band,
and the phase lag it adds. Among candidates the board can carry and that meet the
noise target, the one with the least lag wins; the low-pass is raised as far as the
target allows, which is the payoff of good notching (step 5).

Until a platform noise target is calibrated (knowledge/rules/filters.json), the
target is the current settings' own noise: a candidate never lets more through.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.filters.biquad import lowpass
from flight_insights.filters.harmonic import (
    ALL_IMUS,
    DOUBLE,
    ESC,
    MULTI_SOURCE,
    QUINTUPLE,
    TRIPLE,
    HarmonicNotch,
    NotchParams,
)
from flight_insights.filters.tracking import Sources, sources
from flight_insights.spectral.welch import Spectrum

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
COMPOSITE_BITS = DOUBLE | TRIPLE | QUINTUPLE
SET = "INS_HNTCH_"
OTHER_SET = "INS_HNTC2_"


@dataclass(frozen=True)
class Setting:
    notch: Mapping[str, float]  # the INS_HNTCH_ set, by suffix (ENABLE, MODE, FREQ...)
    gyro_filter: float


@dataclass(frozen=True)
class Scored:
    setting: Setting
    noise_db: float  # power let through above the control band, dB re (deg/s)^2
    lag_deg: float  # phase lag added at the lag frequency
    notches: int


@dataclass(frozen=True)
class Outcome:
    current: Scored
    best: Scored | None  # None: nothing beats the current settings by enough
    target_db: float
    candidates: int
    feasible: int


def notch_count(p: NotchParams, n_sources: int, imus: int) -> int:
    """Notches the firmware runs for this set (AP_InertialSensor::init's count)."""
    if not p.enable or not p.num_harmonics:
        return 0
    return n_sources * p.num_harmonics * p.composite * (imus if p.opts & ALL_IMUS else 1)


def _rows(src: Sources, start_us: int, end_us: int, limit: int) -> Floats:
    rows = src.sampled(start_us, end_us)
    if len(rows) > limit:
        rows = rows[np.linspace(0, len(rows) - 1, limit).astype(int)]
    return rows


def _set_gain(
    notch: HarmonicNotch, rows: Floats, freqs: Floats, lag_hz: float
) -> tuple[Floats, float]:
    """Mean |H|^2 of one notch set over the source rows, and its phase at lag_hz at the
    median row."""
    acc = np.zeros(len(freqs))
    for row in rows:
        h = np.ones(len(freqs), np.complex128)
        for n in notch.notches([float(v) for v in row]):
            h = h * n.filter.response(freqs, notch.sample_hz)
        acc += np.abs(h) ** 2
    median = [float(v) for v in np.median(rows, axis=0)]
    phase = 1.0 + 0j
    for n in notch.notches(median):
        phase *= complex(n.filter.response(np.array([lag_hz]), notch.sample_hz)[0])
    return acc / max(len(rows), 1), float(-np.degrees(np.angle(phase)))


@dataclass
class Screen:
    """The flight's hover spectrum and tracking sources, ready to score settings."""

    spectrum: Spectrum
    params: Mapping[str, float]
    tables: Mapping[str, pa.Table]
    spans: Sequence[tuple[int, int]]
    family: str | None
    imus: int
    rules: Mapping[str, Any]

    def __post_init__(self) -> None:
        self.first, self.last = self.spans[0][0], self.spans[-1][1]
        f = self.spectrum.freqs
        top = 0.45 * self.spectrum.rate_hz
        self.band = (f >= float(self.rules["noise_band_low_hz"])) & (f < top)
        self.pre = self.spectrum.psd.sum(axis=0)
        self.lag_hz = float(self.rules["lag_hz"])
        other = NotchParams.read(self.params, OTHER_SET)
        self.other_gain: Floats = np.ones(len(f))
        self.other_lag = 0.0
        self.other_count = 0
        if other.enable and other.num_harmonics:
            notch = HarmonicNotch(other, self.spectrum.rate_hz, self.family)
            src = sources(notch, dict(self.tables), self.first, self.last, 1)
            if src is not None:
                rows = _rows(src, self.first, self.last, int(self.rules["screening_rows"]))
                self.other_gain, self.other_lag = _set_gain(notch, rows, f, self.lag_hz)
                self.other_count = notch_count(other, rows.shape[1], self.imus)

    def notch_part(self, p: NotchParams) -> tuple[Floats, float, int] | None:
        """Gain, lag and notch count of the main set; None if the log lacks its source."""
        if not p.enable or not p.num_harmonics:
            return np.ones(len(self.spectrum.freqs)), 0.0, 0
        notch = HarmonicNotch(p, self.spectrum.rate_hz, self.family)
        src = sources(notch, dict(self.tables), self.first, self.last, 0)
        if src is None:
            return None
        rows = _rows(src, self.first, self.last, int(self.rules["screening_rows"]))
        gain, lag = _set_gain(notch, rows, self.spectrum.freqs, self.lag_hz)
        return gain, lag, notch_count(p, rows.shape[1], self.imus)

    def lowpass_part(self, cutoff: float) -> tuple[Floats, float]:
        lp = lowpass(self.spectrum.rate_hz, cutoff)
        if lp is None:
            return np.ones(len(self.spectrum.freqs)), 0.0
        gain = np.abs(lp.response(self.spectrum.freqs, self.spectrum.rate_hz)) ** 2
        phase = lp.response(np.array([self.lag_hz]), self.spectrum.rate_hz)[0]
        return gain, float(-np.degrees(np.angle(phase)))

    def score(
        self, setting: Setting, part: tuple[Floats, float, int], lp: tuple[Floats, float]
    ) -> Scored:
        gain = part[0] * self.other_gain * lp[0]
        df = self.spectrum.resolution_hz
        noise = float(np.sum(self.pre[self.band] * gain[self.band]) * df)
        return Scored(
            setting,
            float(10 * np.log10(max(noise, 1e-30))),
            part[1] + self.other_lag + lp[1],
            part[2] + self.other_count,
        )


def _composites(base_opts: int, multi: bool) -> list[int]:
    plain = base_opts & ~COMPOSITE_BITS
    if multi:  # SPEC: no double or triple notch with multi-source
        return [plain | MULTI_SOURCE]
    plain &= ~MULTI_SOURCE
    return [plain, plain | DOUBLE, plain | TRIPLE]


def candidates(
    base: Mapping[str, float],
    energy_mask: int,
    current: NotchParams,
    motors: int,
    rules: Mapping[str, Any],
) -> list[dict[str, float]]:
    """Notch sets to try around `base` (the chosen source's MODE, REF and FREQ)."""
    freq = float(base["FREQ"])
    masks = sorted({energy_mask, 1, energy_mask | (current.harmonics if current.enable else 0)})
    multis = [False, True] if int(base["MODE"]) == ESC and motors > 1 else [False]
    out: list[dict[str, float]] = []
    for mask, multi in itertools.product(masks, multis):
        shares = rules["per_motor_bandwidth_share"] if multi else rules["bandwidth_share"]
        for opts in _composites(int(base.get("OPTS", current.opts)), multi):
            for share in shares:
                out.append(
                    {
                        **{k: float(v) for k, v in base.items()},
                        "ENABLE": 1.0,
                        "HMNCS": float(mask),
                        "OPTS": float(opts),
                        "BW": float(max(5, round(freq * share))),
                    }
                )
    return out


def search(
    screen: Screen,
    base: Mapping[str, float],
    energy_mask: int,
    motors: int,
    cpu_load_p95: float | None,
) -> Outcome:
    rules = screen.rules
    params = screen.params
    current_p = NotchParams.read(params, SET)
    current_filter = float(params.get("INS_GYRO_FILTER", 0))
    current_set = {
        "ENABLE": float(current_p.enable), "MODE": float(current_p.mode), "FREQ": current_p.freq,
        "BW": current_p.bw, "REF": current_p.ref, "HMNCS": float(current_p.harmonics),
        "OPTS": float(current_p.opts), "ATT": current_p.att, "FM_RAT": current_p.fm_rat,
    }  # fmt: skip
    current_part = screen.notch_part(current_p) or (np.ones(len(screen.spectrum.freqs)), 0.0, 0)
    current = screen.score(
        Setting(current_set, current_filter), current_part, screen.lowpass_part(current_filter)
    )
    target = rules.get("noise_target_db")
    target_db = float(target) if target is not None else current.noise_db
    cap = int(rules["max_notches"].get(screen.family or "", rules["max_notches"]["F4"]))
    if cpu_load_p95 is not None and cpu_load_p95 >= float(rules["busy_cpu_pct"]):
        cap = min(cap, current.notches)
    cutoffs = sorted({*map(float, rules["lowpass_hz"]), current_filter} - {0.0})
    lows = {c: screen.lowpass_part(c) for c in cutoffs}
    scored: list[Scored] = []
    tried = 0
    for notch_set in candidates(base, energy_mask, current_p, motors, rules):
        p = current_p.with_changes(
            enable=bool(notch_set["ENABLE"]), mode=int(notch_set["MODE"]), freq=notch_set["FREQ"],
            bw=notch_set["BW"], ref=notch_set.get("REF", current_p.ref),
            harmonics=int(notch_set["HMNCS"]), opts=int(notch_set["OPTS"]),
        )  # fmt: skip
        part = screen.notch_part(p)
        if part is None:
            continue
        full = {**current_set, **notch_set}
        for c, lp in lows.items():
            tried += 1
            s = screen.score(Setting(full, c), part, lp)
            if s.notches <= cap:
                scored.append(s)
    feasible = [s for s in scored if s.noise_db <= target_db + 1e-9]
    best = min(feasible, key=lambda s: (round(s.lag_deg, 3), s.notches, s.noise_db), default=None)
    worth = rules["worth_it"]
    if best is not None:
        lag_gain = current.lag_deg - best.lag_deg
        noise_gain = current.noise_db - best.noise_db
        if not (
            lag_gain >= float(worth["lag_deg"])
            or (noise_gain >= float(worth["noise_db"]) and lag_gain >= -0.5)
        ):
            best = None
    return Outcome(current, best, target_db, tried, len(feasible))
