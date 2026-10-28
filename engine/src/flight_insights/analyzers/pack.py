"""Each pack's capacity and resistance from one flight (spec: Power and efficiency
analysis, "The battery's usable energy"; rules in `knowledge/rules/ocv.json` "fit").

- **Which packs.** Each monitor that measures its own pack's voltage and current
  (`battery_power.counted`): one that adds others up or reads the ESCs is no one pack.
- **The fit** (`performance.battery.fit_pack`): the terminal voltage over the monitor's
  rows from arming, modelled as `cells x OCV(z0 - charge used / Q) - R0 I - R1 (I
  low-passed)`, up to the first row below the chemistry's cell cutoff (a pack run flat
  collapses there). The capacity comes out when the flight used enough of the pack;
  otherwise the resistance alone.
- **The rest.** The pack's charge at the start, from its voltage at rest: the last rows
  before arming, else the quietest row just after it (ArduPilot logs nothing before
  arming unless `LOG_DISARMED` is set).
- **What it needs.** The cells and chemistry from the platform setup, and a capacity to
  search around: the monitor's own `BATTn_CAPACITY`, else the setup's.

Each pack gives its evidence; the first (the one the failsafes watch) gives the metrics.
Across flights, `performance/packs.py` reads them: the capacity `BATTn_CAPACITY` should
hold, and how each pack ages.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.battery_power import PREFIXES, counted
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, MetricValue, Result, Segment
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si
from flight_insights.performance.battery import Chemistry, PackFit, chemistry, fit_pack

Array = np.ndarray[Any, Any]
EVIDENCE = "pack:"  # + the monitor's parameter prefix: pack:BATT, pack:BATT2 ...


def _number(answers: Mapping[str, Any], key: str) -> float | None:
    v = answers.get(key)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else None


def rated_mah(flight: FlightData, prefix: str) -> tuple[float, str] | None:
    """The capacity the fit searches around, and where it came from."""
    own = flight.params.get(f"{prefix}_CAPACITY")
    if own is not None and own > 0:
        return float(own), f"{prefix}_CAPACITY"
    setup = _number(flight.answers, "capacity_mah")
    return (setup, "the setup") if setup is not None else None


def rows(t: Array, volts: Array, amps: Array, arm_us: int, disarm_us: int, cells: int,
         chem: Chemistry, spec: Mapping[str, Any]) -> tuple[Array, Array, str] | None:  # fmt: skip
    """The rows to fit (a mask), the rows the rest is read from (indices) and where they
    are; None when nothing is left to fit."""
    armed = (t >= arm_us) & (t <= disarm_us)
    flat = np.flatnonzero(armed & (volts < cells * chem.cutoff))
    use = armed & (t < t[flat[0]]) if len(flat) else armed
    if int(use.sum()) < 3:
        return None
    n = int(spec["rest_samples"])
    before = np.flatnonzero(t < arm_us)
    if len(before) >= n:
        return use, before[-n:], "before arming"
    window = np.flatnonzero(use & (t <= arm_us + float(spec["rest_window_s"]) * 1e6))
    if not len(window):
        return None
    return use, window[[int(np.argmin(amps[window]))]], "after arming"


Fitted = tuple[PackFit, dict[str, Any]]


def _fit_one(flight: FlightData, reg: Registry, prefix: str, t: Array, volts: Array,
             amps: Array, cells: int, chem: Chemistry) -> Fitted | None:  # fmt: skip
    rated = rated_mah(flight, prefix)
    found = rows(t, volts, amps, flight.arm_us, flight.disarm_us, cells, chem,
                 reg.rules["ocv"]["fit"])  # fmt: skip
    if rated is None or found is None:
        return None
    use, rest, rest_from = found
    fit = fit_pack(t[use] / 1e6, volts[use], amps[use], float(np.median(volts[rest])),
                   float(np.median(amps[rest])), cells, chem, rated[0] / 1000.0,
                   reg.rules["ocv"])  # fmt: skip
    values: dict[str, Any] = {
        "monitor": prefix,
        "capacity_mah": None if fit.capacity_ah is None else round(fit.capacity_ah * 1000),
        "rated_mah": round(rated[0]),
        "rated_from": rated[1],
        "label_mah": _number(flight.answers, "capacity_mah"),
        "follows": fit.follows,
        "free": fit.free,
        "more_than_mah": None if fit.more_than_ah is None else round(fit.more_than_ah * 1000),
        "resistance_mohm": round(fit.resistance * 1000, 1),
        "slow_resistance_mohm": round(fit.slow_resistance * 1000, 1),
        "start_pct": round(100 * fit.start_soc, 1),
        "end_pct": round(100 * fit.end_soc, 1),
        "used_mah": round(fit.used_ah * 1000),
        "rms_mv": round(fit.rms_v * 1000),
        "rest": rest_from,
        "cells": cells,
        "chemistry": chem.name,
        "rows": int(use.sum()),
    }
    return fit, values


def _pack(flight: FlightData, reg: Registry) -> Result:
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or not {"Volt", "Curr"} <= set(bat.column_names):
        return Result(skipped="no battery monitor logged (BAT)")
    cells = _number(flight.answers, "cells")
    if cells is None:
        return Result(skipped="the platform setup gives no cell count")
    chem = chemistry(reg.rules["ocv"], str(flight.answers.get("battery_chemistry") or "LiPo"))
    t = np.asarray(bat.column("TimeUS").to_numpy(), np.int64)
    inst = (
        np.asarray(bat.column("Inst").to_numpy(), np.int64)
        if "Inst" in bat.column_names
        else np.zeros(len(t), np.int64)
    )
    volts, amps = si.values(bat, "Volt"), si.values(bat, "Curr")
    use, how = counted(flight.params, sorted({int(i) for i in np.unique(inst)}),
                       reg.rules["batteries"])  # fmt: skip
    if how != "packs":
        return Result(skipped="no monitor measures its own pack")
    out = Result()
    for i in use:
        if not 0 <= i < len(PREFIXES):
            continue
        mine = inst == i
        order = np.argsort(t[mine], kind="stable")
        got = _fit_one(flight, reg, PREFIXES[i], t[mine][order], volts[mine][order],
                       amps[mine][order], int(cells), chem)  # fmt: skip
        if got is None:
            continue
        fit, values = got
        prefix = values["monitor"]
        rated = f"{values['rated_mah']} mAh from {values['rated_from']}"
        if not fit.follows:
            values["capacity_mah"] = None
            capacity = (f"its voltage does not follow the pack's curve (RMS {values['rms_mv']} "
                        f"mV), whatever its capacity near {rated}: nothing is read")  # fmt: skip
        elif fit.capacity_ah is not None:
            capacity = f"{values['capacity_mah']} mAh measured" + (
                f", far from the {rated}" if fit.free else f", near the {rated}"
            )
        elif fit.more_than_ah is not None:
            capacity = (f"holds more than {values['more_than_mah']} mAh, more than this flight "
                        f"can show, far from the {rated}")  # fmt: skip
        else:
            capacity = f"too little used ({values['used_mah']} mAh) to measure its capacity"
        ev = Evidence(
            id=EVIDENCE + prefix,
            kind="series",
            summary=f"{prefix}: {capacity}"
            + (
                f"; resistance {values['resistance_mohm']} mOhm "
                f"(+{values['slow_resistance_mohm']} slow), "
                f"{values['start_pct']}% to {values['end_pct']}%"
                if fit.follows
                else ""
            ),
            messages=("BAT",),
            fields=("Volt", "Curr"),
            segment=Segment("flight", flight.arm_us, flight.disarm_us),
            values=values,
        )
        out.evidence.append(ev)
        if i != use[0] or not fit.follows:
            continue
        if fit.capacity_ah is not None:
            out.metrics.append(MetricValue("pack.capacity_mah", "flight",
                                           fit.capacity_ah * 1000, None, ev.id))  # fmt: skip
        out.metrics.append(MetricValue("pack.resistance_mohm", "flight",
                                       (fit.resistance + fit.slow_resistance) * 1000, None,
                                       ev.id))  # fmt: skip
    if not out.evidence:
        return Result(skipped="no pack had rows to fit and a capacity to search around")
    return out


PACKS = Analyzer(
    name="pack",
    version=1,
    run=_pack,
    metrics=("pack.capacity_mah", "pack.resistance_mohm"),
    findings=(),
)
