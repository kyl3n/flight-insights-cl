"""How heavy the aircraft flew (spec: Vehicle fingerprinting, "Payload estimation without
a scale"): the thrust its motors made in steady hover, at standard air, as a share of
their full thrust. Two flights' values give their mass ratio; one known take-off weight
turns them into kilograms.

- **Thrust from each motor's output.** ArduPilot linearises thrust with
  `MOT_THST_EXPO` (e): a motor's thrust is (1 - e) x + e x^2 of its full thrust, where
  x is its output between `MOT_SPIN_MIN` and `MOT_SPIN_MAX` times the battery voltage
  over `MOT_BAT_VOLT_MAX` (AP_Motors_Thrust_Linearization). The voltage is the one the
  battery measured (`BAT.Volt`), not the firmware's resting estimate: the motors see
  the sagged voltage. The aircraft's thrust is the mean over its motors, so a motor
  working harder to hold attitude counts as it should.
- **Tilt.** Only the vertical part holds the weight: the thrust times cos(roll) cos(pitch).
- **Air density.** A rotor makes thrust in proportion to air density, so the same
  aircraft needs more output on a hot day or a high field. The thrust is scaled to
  standard air (1.225 kg/m3) with the density from the barometer's pressure and
  temperature. The barometer reads the board's temperature, a little above the air's;
  in a ratio between flights of one airframe that offset largely cancels. In thinner
  air a motor also spins a little faster at the same output, so the scaling
  over-corrects slightly; across one airframe's flights density moves by a few percent.

Steady hover is the fingerprint's windows above a height where ground effect no longer
helps, with the pilot's stick inputs taken out. Samples where a motor sits at either end
of its range are left out: there its output no longer says what it was asked for.

The density is worked out on the laptop and never leaves it: the evidence cites the
barometer's pressure, which the location policy coarsens, so the AI path withholds the
evidence and the metric measured from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, MetricValue, Result, Segment
from flight_insights.analyzers.motors_health import motors
from flight_insights.analyzers.rate_health import quiet_hover
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.tune import height
from flight_insights.dataflash import si

Array = np.ndarray[Any, Any]
UNITS = {("BAT", "Volt"): "V", ("ATT", "Roll"): "deg", ("ATT", "Pitch"): "deg",
         ("BARO", "Press"): "Pa", ("BARO", "Temp"): "degC"}  # fmt: skip


def _rules(reg: Registry) -> Mapping[str, Any]:
    rules: Mapping[str, Any] = reg.rules["payload"]
    return rules


def _times(table: Any) -> Array:
    return np.asarray(table.column("TimeUS").to_numpy(), np.int64)


def _instance(table: Any, column: str, wanted: int) -> Array:
    if column not in table.column_names:
        return np.ones(table.num_rows, bool)
    return np.asarray(table.column(column).to_numpy(), np.int64) == wanted


def _reference_volts(flight: FlightData, rules: Mapping[str, Any]) -> tuple[float, str] | None:
    """The voltage the thrust curve's full output is measured at: MOT_BAT_VOLT_MAX, or a
    full charge of the pack's cells when voltage compensation is off."""
    volt_max = float(flight.params.get("MOT_BAT_VOLT_MAX", 0.0))
    if volt_max > 0:
        return volt_max, "MOT_BAT_VOLT_MAX"
    cells = flight.fingerprint.get("cells", {}).get("value")
    if cells:
        return float(cells) * float(rules["full_cell_v"]), f"{int(cells)} cells full"
    return None


def _payload(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to measure")
    if flight.vehicle != "copter":  # QuadPlane's VTOL motors: Phase 3
        return Result(skipped="the payload estimate is for Copter")
    rules = _rules(reg)
    tables = {m: flight.table(m) for m in ("RCOU", "BAT", "ATT", "BARO")}
    missing = [m for m, t in tables.items() if t is None or t.num_rows < 2]
    if missing:
        return Result(skipped=f"the log has no {', '.join(missing)}")
    rcou, bat, att, baro = (tables[m] for m in ("RCOU", "BAT", "ATT", "BARO"))
    assert rcou is not None and bat is not None and att is not None and baro is not None
    for (message, field), wanted in UNITS.items():
        table = tables[message]
        assert table is not None
        if field not in table.column_names or si.unit(table, field) != wanted:
            return Result(skipped=f"{message}.{field} is not logged in {wanted}")
    ms = motors(flight.params)
    names = [f"C{m.channel}" for m in ms]
    if not ms or not set(names) <= set(rcou.column_names):
        return Result(skipped="the motor outputs are not in RCOU")
    reference = _reference_volts(flight, rules)
    if reference is None:
        return Result(skipped="MOT_BAT_VOLT_MAX is off and the cell count is not known")
    volt_ref, volt_ref_from = reference

    t = _times(rcou)
    hover = quiet_hover(flight, reg)
    keep = np.zeros(len(t), bool)
    for a, b in hover:
        keep |= (t >= a) & (t <= b)
    above = height(flight)
    if above is not None:
        keep &= np.interp(t, *above) >= float(rules["min_height_m"])

    p = flight.params
    low = float(p.get("MOT_PWM_MIN", 0)) or float(p.get("RC3_MIN", 1000))
    high = float(p.get("MOT_PWM_MAX", 0)) or float(p.get("RC3_MAX", 2000))
    spin_min, spin_max = float(p.get("MOT_SPIN_MIN", 0.15)), float(p.get("MOT_SPIN_MAX", 0.95))
    if high <= low or spin_max <= spin_min:
        return Result(skipped="the motor output range (MOT_PWM_*, MOT_SPIN_*) is not valid")
    expo = float(np.clip(p.get("MOT_THST_EXPO", 0.65), -1.0, 1.0))
    pwm = np.column_stack([si.values(rcou, n) for n in names])
    out = ((pwm - low) / (high - low) - spin_min) / (spin_max - spin_min)
    edge = float(rules["saturated"])
    keep &= ((out > edge) & (out < 1.0 - edge)).all(axis=1)

    battery = _instance(bat, "Inst", int(p.get("MOT_BAT_IDX", 0)))
    sensor = _instance(baro, "I", int(p.get("BARO_PRIMARY", 0)))
    if battery.sum() < 2 or sensor.sum() < 2:
        return Result(skipped="the log has no battery voltage or barometer for the motors")
    volts = np.interp(t, _times(bat)[battery], si.values(bat, "Volt")[battery])
    keep &= volts > 0
    seconds = float(keep.sum()) * float(np.median(np.diff(t))) / 1e6
    if seconds < float(rules["min_hover_s"]):
        return Result(skipped="too little steady hover, clear of the ground, to weigh the aircraft")

    x = out[keep] * (volts[keep] / volt_ref)[:, None]
    thrust = ((1.0 - expo) * x + expo * x * x).mean(axis=1)
    ta = _times(att)
    tilt = np.cos(np.radians(np.interp(t[keep], ta, si.values(att, "Roll")))) * np.cos(
        np.radians(np.interp(t[keep], ta, si.values(att, "Pitch")))
    )
    tb = _times(baro)[sensor]
    kelvin = np.interp(t[keep], tb, si.values(baro, "Temp")[sensor]) + 273.15
    density = np.interp(t[keep], tb, si.values(baro, "Press")[sensor]) / (
        float(rules["gas_constant"]) * kelvin
    )
    ratio = density / float(rules["standard_density"])
    weighed = thrust * tilt * ratio
    value = 100.0 * float(np.median(weighed))

    windows = [
        round(100.0 * float(np.median(weighed[(t[keep] >= a) & (t[keep] <= b)])), 2)
        for a, b in hover
        if ((t[keep] >= a) & (t[keep] <= b)).any()
    ]
    ev = Evidence(
        id="payload:hover",
        kind="metric",
        summary=(
            f"{value:.1f}% of full thrust at standard air over {seconds:.0f} s of steady "
            f"hover: {len(ms)} motors, expo {expo:g}, {float(np.median(volts[keep])):.1f} V "
            f"against {volt_ref:g} V ({volt_ref_from}), air at "
            f"{float(np.median(ratio)):.3f} of standard"
        ),
        messages=("RCOU", "BAT", "ATT", "BARO", "CTUN"),
        fields=(*names, "Volt", "Roll", "Pitch", "Press", "Temp", "Alt"),
        segment=Segment("hover", hover[0][0], hover[-1][1]),
        values={
            "seconds": round(seconds, 1),
            "motors": len(ms),
            "expo": expo,
            "volts": round(float(np.median(volts[keep])), 2),
            "reference_volts": round(volt_ref, 2),
            "tilt_deg": round(float(np.degrees(np.arccos(np.median(tilt)))), 1),
            "density_ratio": round(float(np.median(ratio)), 4),
            "windows": windows,
        },
    )
    entry = reg.metrics["payload.hover_thrust"]
    metric = MetricValue(entry.id, "hover", value, entry.zone(value), ev.id)
    return Result(metrics=[metric], evidence=[ev])


PAYLOAD = Analyzer(
    name="payload",
    version=1,
    run=_payload,
    metrics=("payload.hover_thrust",),
    findings=(),
)
