"""Piljek's open T-Motor bench data into the validation runner's tables (tools/props/validate.py).

    uv run python tools/props/piljek.py CSV_DIR OUT_DIR

CSV_DIR holds the 24 CSV files of Piljek, P. (2023), "UAV Multirotor Propulsion -
Experimental Data", Harvard Dataverse, https://doi.org/10.7910/DVN/J5L2P4, CC0 1.0: an
RCbenchmark 1780 stand sweeping the ESC's PWM from 1000 to 2000 us in 100 us steps, up and
down twice, 5 s a step, on T-Motor AG1005 90Kv, P80 100Kv and U13 130Kv motors (the U13 on
two ESCs) with G30x10.5 and G32x11 propellers, at 3.8, 4.0 and 4.2 V a cell (12S).

Each table: one setup at one voltage. Each step: the four readings at its PWM averaged
(thrust, torque as its magnitude, optical RPM, voltage, current), the load cell's and the
torque sensor's zero taken from the 1000 us readings, interpolated in time, and taken off;
steps under 300 rpm left out. The ambient temperature is not given for every setup (only
setups 5-8 log the motor's), so every table is read in standard air: the same for all, so it
cancels in the ratios the predictions are made of.
The dataset's description gives setups 5 and 7 as "G32x10.5"; their thrust coefficient on a
30-inch disc (0.079, against 0.081 for the G30 on the other motors and 0.10 for every G32)
shows a G30x10.5, as T-Motor's G series has no 32x10.5.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

G = 9.80665
SETUPS = {  # setup: motor, kv, ESC, diameter, pitch
    1: ("AG1005", 90, "Flame 60A", 30.0, 10.5),
    2: ("AG1005", 90, "Flame 60A", 32.0, 11.0),
    3: ("P80", 100, "Alpha 80A", 30.0, 10.5),
    4: ("P80", 100, "Alpha 80A", 32.0, 11.0),
    5: ("U13", 130, "Alpha 120A", 30.0, 10.5),
    6: ("U13", 130, "Alpha 120A", 32.0, 11.0),
    7: ("U13", 130, "Flame 180A", 30.0, 10.5),
    8: ("U13", 130, "Flame 180A", 32.0, 11.0),
}
CELLS = 12


def _zero(times: list[float], at: list[tuple[float, float]], t: float) -> float:
    """The sensor's zero at time t, between the readings at rest either side."""
    before = [z for z in at if z[0] <= t]
    after = [z for z in at if z[0] >= t]
    if not before or not after:
        return (before or after)[-1 if before else 0][1]
    (t0, z0), (t1, z1) = before[-1], after[0]
    return z0 if t1 == t0 else z0 + (z1 - z0) * (t - t0) / (t1 - t0)


def table(path: Path) -> dict[str, object]:
    m = re.match(r"setup(\d)_measurement_(\d\.\d)V\.csv", path.name)
    if m is None:
        raise ValueError(f"not one of the dataset's files: {path.name}")
    setup, per_cell = int(m.group(1)), float(m.group(2))
    motor, kv, esc, d, p = SETUPS[setup]
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))

    def num(r: dict[str, str], key: str) -> float:
        return float(r[key] or 0.0)

    times = [num(r, "Time (s)") for r in rows]
    rest = [(num(r, "Time (s)"), num(r, "Thrust A (N)"), abs(num(r, "Torque A (N·m)")))
            for r in rows if r["ESC A (µs)"] == "1000"]  # fmt: skip
    steps: dict[int, list[tuple[float, float, float, float, float]]] = {}
    for r in rows:
        t, us = num(r, "Time (s)"), int(float(r["ESC A (µs)"]))
        rpm = num(r, "Motor Optical Speed A (RPM)")
        if us <= 1000 or rpm < 300:
            continue
        thrust = num(r, "Thrust A (N)") - _zero(times, [(a, b) for a, b, _ in rest], t)
        torque = abs(num(r, "Torque A (N·m)")) - _zero(times, [(a, c) for a, _, c in rest], t)
        steps.setdefault(us, []).append(
            (thrust, torque, rpm, num(r, "Voltage A (V)"), num(r, "Current A (A)"))
        )
    out_rows = []
    for us in sorted(steps):
        got = steps[us]
        thrust, torque, rpm, volts, amps = (sum(x[k] for x in got) / len(got) for k in range(5))
        out_rows.append({"throttle_pct": (us - 1000) / 10.0,
                         "thrust_g": round(thrust / G * 1000, 1),
                         "power_w": round(volts * amps, 2), "rpm": round(rpm, 1),
                         "torque_nm": round(torque, 4), "current_a": round(amps, 3),
                         "volts": round(volts, 3)})  # fmt: skip
    return {"motor": f"T-Motor {motor} {kv}KV, {esc}", "family": "tmotor_g", "diameter_in": d,
            "pitch_in": p, "blades": 2, "kv": kv, "volts": round(CELLS * per_cell, 1),
            "ambient_c": None,
            "source": f"Piljek 2023, doi:10.7910/DVN/J5L2P4 (CC0 1.0), {path.name}",
            "rows": out_rows}  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, out = Path(args[0]), Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("setup*_measurement_*V.csv")):
        got = table(path)
        (out / f"{path.stem}.json").write_text(json.dumps(got, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
