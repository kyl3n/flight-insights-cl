"""The battery's power, from every monitor that measures a pack (spec: Power and efficiency
analysis, "Power buckets"; rules in `knowledge/rules/batteries.json`).

ArduPilot logs one BAT row per battery monitor, and an aircraft on parallel packs often
has one monitor on each: the first alone would read part of the power. So:

- **Which monitors.** Each logged monitor (BAT.Inst 0 is `BATT_`, 1 `BATT2_`, up to
  `BATTG_`) is classed by its type parameter. Those that measure their own pack's
  voltage and current count, as does a monitor the parameters do not name. A monitor
  that adds others up, reads the ESCs' telemetry, measures no current or measures
  something other than a flight pack is left out, so nothing counts twice; with no pack
  monitor logged, a sum monitor stands in, else an ESC monitor.
- **Power.** Each counted monitor's volts times amps, read at the first one's rows (its
  own rows interpolated in time), summed. The current is the summed amps; the voltage is
  the first pack's.
- **Energy.** Power integrated over time by the trapezoid rule, every sample used.
  Stretches with no row longer than the rules' gap are bridged the same way and their
  length is reported beside the energy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.dataflash import si

Array = np.ndarray[Any, Any]
# BAT.Inst 0 to 15 and their parameter groups
PREFIXES = ("BATT", *(f"BATT{c}" for c in "23456789ABCDEFG"))


@dataclass(frozen=True)
class Supply:
    """The pack power, at the first counted monitor's rows."""

    t: Array  # log time, µs
    volt: Array  # the first counted pack's voltage, V
    current: Array  # summed current, A
    currents: Array  # (instances, rows) each one's current, A
    power: Array  # summed volts times amps, W
    instances: tuple[int, ...]  # BAT.Inst of the monitors counted
    resistance: Array  # (instances,) each one's last positive BAT.Res while armed, Ohm; nan
    how: str  # 'packs', 'sum' or 'esc': what the power was read from


def monitor_type(params: Mapping[str, float], inst: int) -> int | None:
    """The type parameter of monitor `inst`; None when the parameters do not name it."""
    if not 0 <= inst < len(PREFIXES):
        return None
    value = params.get(f"{PREFIXES[inst]}_MONITOR")
    return None if value is None else int(value)


def counted(params: Mapping[str, float], logged: list[int], rules: Mapping[str, Any]
            ) -> tuple[list[int], str]:  # fmt: skip
    """The monitors whose power is the pack's, and what they are."""
    kind = {i: monitor_type(params, i) for i in logged}
    packs = [i for i in logged if kind[i] is None or kind[i] in rules["pack"]]
    if packs:
        return packs, "packs"
    for how in ("sum", "esc"):
        found = [i for i in logged if kind[i] in rules[how]]
        if found:
            return found[:1], how
    return [], "none"


def supply(flight: FlightData, rules: Mapping[str, Any]) -> Supply | None:
    """The pack power over the whole log; None without a monitor that measures it."""
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or not {"Volt", "Curr"} <= set(bat.column_names):
        return None
    t = np.asarray(bat.column("TimeUS").to_numpy(), np.int64)
    inst = (
        np.asarray(bat.column("Inst").to_numpy(), np.int64)
        if "Inst" in bat.column_names
        else np.zeros(len(t), np.int64)
    )
    volt, curr = si.values(bat, "Volt"), si.values(bat, "Curr")
    res = si.values(bat, "Res") if "Res" in bat.column_names else np.full(len(t), np.nan)
    use, how = counted(flight.params, sorted({int(i) for i in np.unique(inst)}), rules)
    if not use:
        return None
    base = inst == use[0]
    order = np.argsort(t[base], kind="stable")
    bt = t[base][order]
    total_power = np.zeros(len(bt))
    currents: list[Array] = []
    resistance: list[float] = []
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    for i in use:
        rows = inst == i
        ti = t[rows]
        k = np.argsort(ti, kind="stable")
        ti, vi, ai = ti[k], volt[rows][k], curr[rows][k]
        total_power += np.interp(bt, ti, vi * ai)
        currents.append(np.interp(bt, ti, ai))
        r = res[rows & armed]
        good = r[np.isfinite(r) & (r > 0)]
        resistance.append(float(good[-1]) if len(good) else float("nan"))
    return Supply(
        t=bt,
        volt=volt[base][order],
        current=np.sum(currents, axis=0),
        currents=np.array(currents),
        power=total_power,
        instances=tuple(use),
        resistance=np.array(resistance),
        how=how,
    )


def energy_wh(t_us: Array, power_w: Array, start_us: int, end_us: int, max_gap_s: float
              ) -> tuple[float, float]:  # fmt: skip
    """Energy between two log times by the trapezoid rule over every sample, in Wh, and the
    seconds bridged across gaps longer than `max_gap_s`."""
    inside = (t_us >= start_us) & (t_us <= end_us)
    tt, pp = t_us[inside].astype(np.float64) / 1e6, power_w[inside]
    if len(tt) < 2:
        return 0.0, 0.0
    dt = np.diff(tt)
    joules = float(np.sum(dt * (pp[1:] + pp[:-1]) / 2))
    return joules / 3600.0, float(np.sum(dt[dt > max_gap_s]))
