"""A flight pack's usable energy, and its capacity and resistance from flights (spec: Power
and efficiency analysis, "Range, endurance and speed"; rules in `knowledge/rules/ocv.json`).

- **The pack.** Its cells' open-circuit voltage against state of charge (per chemistry),
  its capacity and its series resistance: at a power P it draws
  `I = (E - sqrt(E^2 - 4 R P)) / 2R` from an open-circuit voltage E, and its terminals
  sag to `E - I R`. More power sags it further, so a pack delivers less energy the
  harder it is worked.
- **The discharge.** Flown at a power (constant, or a function of time), step by step,
  until the first of: the cell cutoff, the pack empty, the current or power it can give, the
  firmware's own battery failsafes as it judges them (the voltage it watches under
  `BATT_LOW_VOLT` for `BATT_LOW_TIMER`; the charge it counts from zero at start-up
  reaching `BATT_CAPACITY - BATT_LOW_MAH`), or an energy reserve.
- **The reserve** (the fleet owner's rule, 2026-10-28): the larger of the failsafe's own
  reserve and a share of the energy the pack could give to its cutoff at that power,
  so the discharge stops at whichever comes first.
- **A pack from a flight.** The voltage at rest before arming gives the starting charge;
  the terminal voltage through the flight is fitted as
  `cells x OCV(z0 - charge used / Q) - R0 I - R1 (I low-passed)`, R0 and R1 by least
  squares for each capacity Q, Q searched near the rated capacity with a prior. A flight
  that used little of the pack gives its resistance only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

Array = np.ndarray[Any, Any]
EDGE = 1.01  # a capacity within 1% of a search's edge is at it


@dataclass(frozen=True)
class Chemistry:
    name: str
    soc: Array  # ascending, 0..1
    volts: Array  # per cell, ascending with soc
    cutoff: float  # lowest cell voltage under load, V
    measured_shape: bool
    charge_from_voltage: bool

    def ocv(self, soc: float | Array) -> Array:
        return np.asarray(np.interp(soc, self.soc, self.volts))

    def soc_at(self, volts_per_cell: float | Array) -> Array:
        return np.asarray(np.interp(volts_per_cell, self.volts, self.soc))


def chemistry(rules: Mapping[str, Any], name: str) -> Chemistry:
    """A chemistry's curve: measured, or the measured shape between its own voltages."""
    table: Mapping[str, Any] = rules["chemistries"]
    spec = table.get(name) or table["LiPo"]
    if "shape" in spec:
        base = chemistry(rules, str(spec["shape"]))
        lo, hi = float(base.volts[0]), float(base.volts[-1])
        scale = (float(spec["full_v"]) - float(spec["empty_v"])) / (hi - lo)
        volts = float(spec["empty_v"]) + (base.volts - lo) * scale
        soc = base.soc
    else:
        volts = np.asarray(spec["volts"], np.float64)
        soc = np.asarray(spec["soc"], np.float64)
    return Chemistry(
        name=name if name in table else "LiPo",
        soc=soc,
        volts=volts,
        cutoff=float(spec["cutoff_v"]),
        measured_shape=bool(spec.get("measured_shape", False)),
        charge_from_voltage=bool(spec.get("charge_from_voltage", True)),
    )


@dataclass(frozen=True)
class Pack:
    chemistry: Chemistry
    cells: int
    capacity_ah: float
    resistance: float  # whole pack, Ohm

    def open_circuit(self, soc: float | Array) -> Array:
        return self.cells * self.chemistry.ocv(soc)


@dataclass(frozen=True)
class Failsafe:
    """The firmware's battery failsafe as configured; None where off."""

    low_volt: float | None = None
    timer_s: float = 10.0
    resting: bool = False  # BATT_FS_VOLTSRC 1: the sag-compensated voltage
    capacity_mah: float | None = None  # BATT_CAPACITY, as the firmware counts down from it
    low_mah: float | None = None
    current_limit_a: float | None = None


@dataclass(frozen=True)
class Discharge:
    seconds: float
    energy_wh: float  # delivered at the terminals
    charge_ah: float
    end_soc: float
    # cutoff, empty, voltage_failsafe, capacity_failsafe, reserve, current, power, time
    stopped_by: str


def discharge(
    pack: Pack,
    start_soc: float,
    power: float | Callable[[float], float],
    failsafe: Failsafe | None = None,
    stop_energy_wh: float | None = None,
    dt: float = 1.0,
    max_s: float = 6 * 3600.0,
) -> Discharge:
    """The pack flown at `power` W (or `power(t)`) from `start_soc` until something stops it."""
    fs = failsafe or Failsafe()
    soc, t, energy, charge = float(start_soc), 0.0, 0.0, 0.0
    low_since: float | None = None
    budget_ah = (
        (fs.capacity_mah - fs.low_mah) / 1000.0
        if fs.capacity_mah is not None and fs.low_mah is not None
        else None
    )
    stopped = "time"
    while t < max_s:
        if soc <= float(pack.chemistry.soc[0]):
            stopped = "empty"
            break
        p = float(power(t)) if callable(power) else float(power)
        e = float(pack.open_circuit(soc))
        r = pack.resistance
        if r > 0:
            disc = e * e - 4 * r * p
            if disc < 0:
                stopped = "power"
                break
            amps = (e - np.sqrt(disc)) / (2 * r)
        else:
            amps = p / e
        volts = e - amps * r
        if fs.current_limit_a is not None and amps > fs.current_limit_a:
            stopped = "current"
            break
        if volts / pack.cells < pack.chemistry.cutoff:
            stopped = "cutoff"
            break
        if fs.low_volt is not None:
            watched = volts + amps * r if fs.resting else volts
            if watched < fs.low_volt:
                low_since = t if low_since is None else low_since
                if t - low_since >= fs.timer_s:
                    stopped = "voltage_failsafe"
                    break
            else:
                low_since = None
        if budget_ah is not None and charge >= budget_ah:
            stopped = "capacity_failsafe"
            break
        if stop_energy_wh is not None and energy >= stop_energy_wh:
            stopped = "reserve"
            break
        energy += volts * amps * dt / 3600.0
        charge += amps * dt / 3600.0
        soc -= amps * dt / 3600.0 / pack.capacity_ah
        t += dt
    return Discharge(t, energy, charge, soc, stopped)


def usable(
    pack: Pack,
    start_soc: float,
    power: float | Callable[[float], float],
    failsafe: Failsafe | None,
    reserve_share: float,
    dt: float = 1.0,
) -> Discharge:
    """What the pack gives before the reserve: the larger of the failsafe's own reserve and
    `reserve_share` of the energy it could give to its cutoff at this power."""
    to_cutoff = discharge(pack, start_soc, power, None, None, dt)
    keep = (1.0 - min(max(reserve_share, 0.0), 0.9)) * to_cutoff.energy_wh
    return discharge(pack, start_soc, power, failsafe, keep, dt)


@dataclass(frozen=True)
class Usable:
    """What a pack gives before its reserve against a steady load: the energy at the
    terminals at each power on a grid, read between them on a log scale. Below the grid it
    reads the grid's lightest load (less than the pack would give), above it nothing: both
    err short."""

    powers: Array  # W, ascending
    energy_wh: Array  # 0 where the pack cannot give that power
    stopped_by: tuple[str, ...]

    def at(self, power: float | Array) -> Array:
        p = np.asarray(power, np.float64)
        e = np.interp(np.log(np.maximum(p, 1e-9)), np.log(self.powers), self.energy_wh)
        return np.asarray(np.where(p > self.powers[-1] * (1 + 1e-9), 0.0, e))

    def seconds(self, power: float | Array) -> Array:
        p = np.asarray(power, np.float64)
        return np.asarray(self.at(p) * 3600.0 / np.maximum(p, 1e-9))

    def stopped(self, power: float) -> str:
        """What ends a steady flight at this load: the reason at the grid's nearest load
        at or above it (the one `at` errs toward), or "power" above the grid."""
        if power > float(self.powers[-1]) * (1 + 1e-9):
            return "power"
        return self.stopped_by[min(int(np.searchsorted(self.powers, power)), len(self.powers) - 1)]


def usable_curve(
    pack: Pack,
    start_soc: float,
    failsafe: Failsafe | None,
    reserve_share: float,
    low_w: float,
    high_w: float,
    points: int = 24,
    steps: int = 1000,
) -> Usable:
    """`usable` at `points` loads from `low_w` to `high_w`, each discharge in about `steps`
    steps, and at least five to the voltage failsafe's timer."""
    powers = np.geomspace(low_w, high_w, points)
    socs = np.linspace(float(pack.chemistry.soc[0]), start_soc, 64)
    held_wh = float(np.trapezoid(pack.open_circuit(socs), socs)) * pack.capacity_ah
    fs = failsafe or Failsafe()
    finest = fs.timer_s / 5 if fs.low_volt is not None and fs.timer_s > 0 else float("inf")
    energy: list[float] = []
    stopped: list[str] = []
    for p in powers:
        dt = min(max(held_wh * 3600 / float(p) / steps, 0.05), finest)
        d = usable(pack, start_soc, float(p), failsafe, reserve_share, dt)
        energy.append(d.energy_wh)
        stopped.append(d.stopped_by)
    return Usable(powers, np.asarray(energy, np.float64), tuple(stopped))


@dataclass(frozen=True)
class PackFit:
    capacity_ah: float | None  # None when the flight cannot tell it (see `more_than_ah`)
    resistance: float  # R0, Ohm
    slow_resistance: float  # R1, Ohm (the RC branch)
    start_soc: float
    end_soc: float
    used_ah: float
    rms_v: float
    # the voltage followed within the rules' error per cell: when not, nothing it gives
    # is to be trusted, and the rated capacity is far from the pack's
    follows: bool = True
    searched: tuple[float, float] = (0.0, 0.0)  # the capacities searched, Ah
    free: bool = False  # searched without the rated capacity's prior
    more_than_ah: float | None = None  # holds more than this, which the flight cannot show


def lowpass(t: Array, x: Array, tau: float) -> Array:
    """First-order low-pass over irregular times, exact for a held input."""
    out = np.empty_like(x)
    out[0] = x[0]
    for k in range(1, len(x)):
        a = np.exp(-(t[k] - t[k - 1]) / tau)
        out[k] = a * out[k - 1] + (1 - a) * x[k]
    return out


def _golden(cost: Callable[[float], float], a: float, b: float, steps: int = 80) -> float:
    golden = (np.sqrt(5) - 1) / 2
    c, d = b - golden * (b - a), a + golden * (b - a)
    for _ in range(steps):
        if cost(c) < cost(d):
            b = d
        else:
            a = c
        c, d = b - golden * (b - a), a + golden * (b - a)
    return (a + b) / 2


def fit_pack(
    t_s: Array,
    volts: Array,
    amps: Array,
    rest_volts: float,
    rest_amps: float,
    cells: int,
    chem: Chemistry,
    rated_ah: float,
    rules: Mapping[str, Any],
) -> PackFit:
    """A pack's capacity and resistance from one flight's terminal voltage and current
    (every sample), starting from the voltage at rest before arming.

    The capacity is searched near the rated one, with it as a prior. When that finds the
    capacity at the search's edge, or misses the voltage by more than the rules allow a
    cell, the rated capacity is far from the pack's: the search is made again without the
    prior, from the least the pack can hold (the charge used, over the charge it started
    with) to the most a flight that used `min_soc_span` of it shows."""
    spec: Mapping[str, Any] = rules["fit"]
    t = np.asarray(t_s, np.float64)
    v = np.asarray(volts, np.float64)
    i = np.asarray(amps, np.float64)
    used = np.concatenate([[0.0], np.cumsum(np.diff(t) * (i[1:] + i[:-1]) / 2)]) / 3600.0
    slow = lowpass(t, i, float(spec["tau_s"]))
    total = float(used[-1])
    span_min = float(spec["min_soc_span"])
    gate = float(spec["max_rms_cell_v"]) * cells

    def solve(q: float, z0: float) -> tuple[float, float, float]:
        e = cells * chem.ocv(z0 - used / q)
        a = np.stack([i, slow], axis=1)
        coef, *_ = np.linalg.lstsq(a, e - v, rcond=None)
        r0, r1 = (max(float(c), 0.0) for c in coef)
        resid = e - r0 * i - r1 * slow - v
        return float(np.sum(resid**2)), r0, r1

    # the resting voltage reads the open-circuit voltage less the avionics' small draw;
    # one pass with the flight's resistance corrects it
    z0 = float(chem.soc_at(rest_volts / cells))
    lo, hi = (float(x) * rated_ah for x in spec["capacity_range"])
    sigma = float(spec["capacity_spread"]) * rated_ah
    n = len(t)
    _, r0, _ = solve(rated_ah, z0)
    z0 = float(chem.soc_at((rest_volts + rest_amps * r0) / cells))

    def rms(sse: float) -> float:
        return float(np.sqrt(sse / n))

    def result(q: float | None, at: float, searched: tuple[float, float], free: bool,
               more: float | None = None) -> PackFit:  # fmt: skip
        sse, r0, r1 = solve(at, z0)
        return PackFit(q, r0, r1, z0, z0 - total / at, total, rms(sse), rms(sse) <= gate,
                       searched, free, more)  # fmt: skip

    def cost(q: float) -> float:
        sse = solve(q, z0)[0]
        # the residual variance per sample against the capacity prior, as a log-likelihood
        return n * np.log(max(sse / n, 1e-12)) + ((q - rated_ah) / sigma) ** 2

    if not chem.charge_from_voltage:
        return result(None, rated_ah, (rated_ah, rated_ah), False)
    if total / rated_ah >= span_min:
        best = _golden(cost, lo, hi)
        first = result(best, best, (lo, hi), False)
        inside = lo * EDGE < best < hi / EDGE
    else:
        first = result(None, rated_ah, (rated_ah, rated_ah), False)
        inside = True
    if inside and first.follows:
        return first
    # the rated capacity is far off: search from the least the pack can hold
    floor = total / max(z0, 1e-6)
    ceiling = total / span_min
    if ceiling <= floor * EDGE:
        return first
    best = _golden(lambda q: solve(q, z0)[0], floor, ceiling)
    if best >= ceiling / EDGE:  # more than the flight can show: no capacity
        return result(None, ceiling, (floor, ceiling), True, ceiling)
    return result(best, best, (floor, ceiling), True)
