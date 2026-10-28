"""An ESC's temperature from its current, fitted per ESC from a flight's own telemetry
(spec: Power and efficiency analysis, "Range, endurance and speed", "The speed build-up":
ESC temperature from a thermal model; rules in `knowledge/rules/performance.json`
"thermal").

- **The model.** The ESC makes heat as the square of its current (conduction in its
  switches) and gives it to the air in proportion to how much hotter it is:
  `dT/dt = (T_air + k I^2 - T) / tau`. Held at a current I it settles at `T_air + k I^2`,
  and gets 63% of the way there in tau.
- **The fit.** The temperature the model gives from the flight's own current, against the
  temperature logged (an output-error fit: the telemetry's whole-degree steps are noise
  on what is compared, not on what predicts it, so they do not bias the answer). For a
  given tau the model is linear in the starting temperature, the air's and k:
  `T = T0 a^n + T_air (1 - a^n) + k F`, with `a = exp(-step / tau)` and F the current
  squared through the same lag; so each tau on a grid is a least-squares solve, and the
  best is refined between its neighbours. A flight whose current hardly changes cannot
  tell the heat it makes from the air's temperature, and a best tau at the grid's end
  cannot be told either: neither gives a model.
- **Prediction.** A run of D seconds at a current I from T0 reaches
  `T_ss + (T0 - T_ss) exp(-D / tau)`. The cooling is the flight's own airflow: a faster
  run cools a little better, so this errs hot. Above the highest current flown it is
  extrapolated, and says so.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.dataflash import si

Array = np.ndarray[Any, Any]


@dataclass(frozen=True)
class Thermal:
    instance: int  # ESC telemetry instance: output channel instance + 1
    tau_s: float
    k: float  # degC per A^2
    air_c: float
    rms_c: float  # the one-step residual
    current_max_a: float  # the highest current flown

    def steady(self, amps: float) -> float:
        """Where it settles held at `amps`."""
        return self.air_c + self.k * amps * amps

    def after(self, amps: float, seconds: float, start_c: float) -> float:
        """Its temperature after `seconds` at `amps` from `start_c`."""
        settle = self.steady(amps)
        return settle + (start_c - settle) * math.exp(-seconds / self.tau_s)


def _design(squared: Array, a: float) -> Array:
    """The model's columns for T0, the air and k at `a = exp(-step / tau)`."""
    lagged = np.empty(len(squared))
    lagged[0] = 0.0
    for j in range(1, len(squared)):
        lagged[j] = a * lagged[j - 1] + (1.0 - a) * squared[j - 1]
    decay = a ** np.arange(len(squared), dtype=np.float64)
    return np.column_stack([decay, 1.0 - decay, lagged])


def _solve(temp: Array, squared: Array, a: float) -> tuple[float, Array]:
    x = _design(squared, a)
    coef, *_ = np.linalg.lstsq(x, temp, rcond=None)
    return float(np.sqrt(np.mean((x @ coef - temp) ** 2))), coef


def fit(t_us: Array, temp_c: Array, amps: Array, rules: Mapping[str, Any],
        instance: int = 0) -> Thermal | None:  # fmt: skip
    """The model from one ESC's rows, or None when the flight cannot tell it."""
    spec: Mapping[str, Any] = rules["thermal"]
    step = float(spec["step_s"])
    order = np.argsort(t_us, kind="stable")
    t = (np.asarray(t_us, np.float64)[order] - float(t_us[order[0]])) / 1e6
    if len(t) < 2 or t[-1] < float(spec["min_seconds"]):
        return None
    grid = np.arange(0.0, t[-1], step)
    temp = np.interp(grid, t, np.asarray(temp_c, np.float64)[order])
    squared = np.interp(grid, t, np.asarray(amps, np.float64)[order] ** 2)
    if float(np.ptp(np.sqrt(squared))) < float(spec["min_current_span_a"]):
        return None
    taus = np.geomspace(float(spec["tau_min_s"]), float(spec["tau_max_s"]), int(spec["tau_steps"]))
    costs = [_solve(temp, squared, math.exp(-step / tau))[0] for tau in taus]
    k = int(np.argmin(costs))
    if k in (0, len(taus) - 1):
        return None
    low, high = math.log(taus[k - 1]), math.log(taus[k + 1])
    golden = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(40):  # golden section on log tau
        x1, x2 = high - golden * (high - low), low + golden * (high - low)
        c1 = _solve(temp, squared, math.exp(-step / math.exp(x1)))[0]
        c2 = _solve(temp, squared, math.exp(-step / math.exp(x2)))[0]
        low, high = (low, x2) if c1 < c2 else (x1, high)
    tau = math.exp((low + high) / 2.0)
    rms, (_, air, heat) = _solve(temp, squared, math.exp(-step / tau))
    if heat <= 0.0:
        return None
    return Thermal(instance=instance, tau_s=tau, k=float(heat), air_c=float(air), rms_c=rms,
                   current_max_a=float(np.max(amps)))  # fmt: skip


def from_flight(flight: FlightData, rules: Mapping[str, Any]) -> list[Thermal]:
    """A model for each ESC whose telemetry gives temperature and current while armed. Not
    from ArduPilot's simulator, whose ESC temperatures are placeholders."""
    if flight.simulated:
        return []
    esc = flight.table("ESC", ["TimeUS", "Instance", "Temp", "Curr"])
    if esc is None or not esc.num_rows or not {"Temp", "Curr"} <= set(esc.column_names):
        return []
    t = np.asarray(esc.column("TimeUS").to_numpy(), np.int64)
    inst = np.asarray(esc.column("Instance").to_numpy(), np.int64)
    temp, amps = si.values(esc, "Temp"), si.values(esc, "Curr")
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    out: list[Thermal] = []
    for i in np.unique(inst[armed]).tolist():
        rows = armed & (inst == i)
        found = fit(t[rows], temp[rows], amps[rows], rules, int(i))
        if found is not None:
            out.append(found)
    return out
