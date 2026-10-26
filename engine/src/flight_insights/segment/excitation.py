"""Excitation events: the stick inputs a rate loop can be measured on (spec: Copter
analyzers, "Rate-loop tune").

An event is a stretch where one axis's rate target (RATE.RDes, PDes or YDes) moves
well away from zero, from the first moment it does to a second after it settles back,
with the lobes of one doublet (one way, then the other) joined into one event. How
closely the measured rate follows the target over the event says how well the rate
loop tracks.

An event counts only when it measures the rate loop and nothing else. It is left out
when:

- the log has a gap in it;
- it was flown in a mode whose sticks do not command lean angle or rate directly
  (AutoTune twitches by itself; Loiter and Auto shape their own targets), or the mode
  changed during it;
- the aircraft was close to the ground;
- the rate controller reported its output saturated (the PID's limit flag, set when
  the motors cannot give what it asks): the aircraft ran out of authority, so the
  error says nothing about the gains;
- the pilot's stick for that axis did not move, where the log holds the sticks: a
  gust moved the aircraft and the controller answered it.

The thresholds come from the knowledge pack (rules/tune.json).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

AXES = ("roll", "pitch", "yaw")
RATE_FIELDS: Mapping[str, tuple[str, str]] = {
    "roll": ("RDes", "R"), "pitch": ("PDes", "P"), "yaw": ("YDes", "Y"),
}  # fmt: skip
PID_MESSAGES: Mapping[str, str] = {"roll": "PIDR", "pitch": "PIDP", "yaw": "PIDY"}
REASONS = ("log gap", "mode", "mode change", "low", "saturated", "no stick")

Array = np.ndarray[Any, Any]


@dataclass(frozen=True)
class Event:
    axis: str
    start_us: int
    end_us: int
    peak_dps: float  # the largest rate target, either way
    stick: float | None  # the largest stick deflection, a fraction of its throw; None: not logged
    error: float  # RMS(target - actual) / RMS(target) over the event
    rejected: str | None = None  # one of REASONS; None when it counts


@dataclass(frozen=True)
class Signals:
    """What can rule an event out, as the log recorded it."""

    modes: Sequence[tuple[int, int, str]]  # (start, end, mode name), log time
    limited: Array | None  # times the axis's rate PID was saturated; None: PIDs not logged
    height: tuple[Array, Array] | None  # times, metres above take-off
    stick: tuple[Array, Array] | None  # times, deflection as a fraction of the stick's throw


def spans(t: Array, target: Array, rules: Mapping[str, Any], axis: str) -> list[tuple[int, int]]:
    """Where the target is driven: runs past the threshold, joined when they are close,
    kept when long enough, then widened to take in the start and the settling."""
    active = np.abs(target) >= float(rules["threshold_dps"][axis])
    if not active.any():
        return []
    edges = np.flatnonzero(np.diff(active.astype(np.int8)))
    starts = list(edges[~active[edges]] + 1)
    ends = list(edges[active[edges]] + 1)
    if active[0]:
        starts.insert(0, 0)
    if active[-1]:
        ends.append(len(t))
    runs: list[list[int]] = []
    merge_us = float(rules["merge_s"]) * 1e6
    for a, b in zip(starts, ends, strict=True):
        if runs and t[a] - t[runs[-1][1] - 1] <= merge_us:
            runs[-1][1] = b
        else:
            runs.append([a, b])
    min_us = float(rules["min_active_s"]) * 1e6
    kept = [(int(t[a]), int(t[b - 1])) for a, b in runs if t[b - 1] - t[a] >= min_us]
    before, after = float(rules["before_s"]) * 1e6, float(rules["after_s"]) * 1e6
    out: list[tuple[int, int]] = []
    for i, (a, b) in enumerate(kept):
        start = max(int(a - before), int(t[0]), out[-1][1] if out else int(t[0]))
        end = min(int(b + after), int(t[-1]))
        if i + 1 < len(kept):
            end = min(end, int(kept[i + 1][0] - before))
        out.append((start, max(end, b)))
    return out


def _error(target: Array, actual: Array) -> float:
    return float(np.sqrt(np.mean((target - actual) ** 2)) / np.sqrt(np.mean(target**2)))


def _why_not(
    a: int, b: int, t: Array, signals: Signals, rules: Mapping[str, Any], period_us: float
) -> str | None:
    if len(t) > 1 and float(np.diff(t).max()) > float(rules["gap_periods"]) * period_us:
        return "log gap"
    stick_modes = set(rules["stick_modes"])
    during = [m for m in signals.modes if m[0] < b and m[1] > a]
    if len(during) > 1:
        return "mode change"
    if not during or during[0][2] not in stick_modes:
        return "mode"
    if signals.height is not None:
        ht, h = signals.height
        inside = (ht >= a) & (ht <= b)
        if inside.any() and float(np.nanmin(h[inside])) < float(rules["min_height_m"]):
            return "low"
    if signals.limited is not None and bool(
        ((signals.limited >= a) & (signals.limited <= b)).any()
    ):
        return "saturated"
    if signals.stick is not None:
        st, s = signals.stick
        inside = (st >= a) & (st <= b)
        if inside.any() and float(np.abs(s[inside]).max()) < float(rules["stick_fraction"]):
            return "no stick"
    return None


def events(
    axis: str, t: Array, target: Array, actual: Array, signals: Signals, rules: Mapping[str, Any]
) -> list[Event]:
    """Every event on one axis, with whether it counts. `t` in log time (µs), the rates
    in degrees a second, as RATE logs them."""
    if len(t) < 3:
        return []
    period_us = float(np.median(np.diff(t)))
    out: list[Event] = []
    for a, b in spans(t, target, rules, axis):
        sel = (t >= a) & (t <= b)
        d, m = target[sel], actual[sel]
        stick = None
        if signals.stick is not None:
            st, s = signals.stick
            inside = (st >= a) & (st <= b)
            stick = float(np.abs(s[inside]).max()) if inside.any() else None
        out.append(
            Event(
                axis=axis,
                start_us=a,
                end_us=b,
                peak_dps=float(np.abs(d).max()),
                stick=stick,
                error=_error(d, m),
                rejected=_why_not(a, b, t[sel], signals, rules, period_us),
            )
        )
    return out
