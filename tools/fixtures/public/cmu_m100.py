"""Position-free window averages from the CMU DJI Matrice 100 data set, for the power
model's check against an airframe flown with an anemometer (spec: Power and efficiency
analysis, "Range, endurance and speed", "Verification").

Source: Rodrigues, T. A. et al., "In-flight positional and energy use data set of a DJI
Matrice 100 quadcopter for small package delivery", Scientific Data 8, 155 (2021),
doi:10.1038/s41597-021-00930-x; data: "Data Collected with Package Delivery Quadcopter
Drone", Carnegie Mellon University KiltHub, doi:10.1184/R1/12683453.v1, CC BY 4.0.

    uv run tools/fixtures/public/cmu_m100.py PARAMETERS_CSV FLIGHTS_ZIP OUT_CSV

Reads `parameters.csv` and `flights.zip` as downloaded (never committed: they hold the
aircraft's positions) and writes one row per steady 2 s window: the flight, payload and
take-off mass, the phase, seconds, airspeed, the anemometer's raw reading, ground speed,
climb, the pack's power and voltage. No position, height, heading, time or date leaves
this script; the take-off point's elevation only sets one air density for every row,
rounded.

- Ground velocity: the body-frame velocity rotated by the attitude quaternion (east,
  north, up).
- Airspeed: the anemometer gives the relative air's magnitude and the bearing it comes
  from. Its magnitude reads high, more so slowly (the paper's Usage Notes), so each
  flight's air vector r = c + k v_ground is fitted by least squares over its moving
  samples: the wind is -c / k, and airspeed |v_ground - wind|. A flight that hardly
  moves takes the median k of the others.
- Phases: the engine's rules (knowledge/rules/energy.json "phases"), with the take-off
  point as the height reference; a window whose power's standard deviation exceeds the
  points' max_power_cv of its mean is left out, as the engine's points do
  (knowledge/rules/performance.json "points").
"""

# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.3.4"]
# ///

from __future__ import annotations

import csv
import io
import json
import math
import sys
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

Array = np.ndarray[Any, Any]

TAKEOFF_KG = 3.680  # airframe, battery, anemometer and computer (the paper, "Airframe")
RULES = Path(__file__).resolve().parents[3] / "knowledge" / "rules"
PHASES: dict[str, Any] = json.loads((RULES / "energy.json").read_text("utf-8"))["phases"]
MAX_POWER_CV = float(
    json.loads((RULES / "performance.json").read_text("utf-8"))["points"]["max_power_cv"]
)
TILE_S = float(PHASES["tile_s"])
MIN_SAMPLES = int(PHASES["min_samples"])
MIN_HEIGHT_M = float(PHASES["min_height_m"])
CLIMB_MS = float(PHASES["climb_ms"])
HOVER_MS = float(PHASES["hover_speed_ms"])
LEVEL_MS = float(PHASES["level_speed_ms"])
LEVEL_CLIMB_MS = float(PHASES["level_climb_ms"])
MAX_ACCEL = float(PHASES["max_accel"])
MAX_TURN_DEG_S = float(PHASES["max_turn_deg_s"])
MIN_MOVING_SAMPLES = 100
K_RANGE = (0.5, 1.5)


def rotate(q: Array, v: Array) -> Array:
    """Each row of v rotated by its quaternion (x, y, z, w)."""
    u, w = q[:, :3], q[:, 3:]
    t = 2.0 * np.cross(u, v)
    return v + w * t + np.cross(u, t)


def isa_density(altitude_m: float) -> float:
    t = 288.15 - 0.0065 * altitude_m
    p = 101325.0 * (t / 288.15) ** 5.25588
    return p / (287.05287 * t)


def wind_fit(air: Array, ground: Array) -> tuple[float, Array] | None:
    """(k, wind) from air = c + k ground over the moving samples, or None."""
    moving = np.hypot(ground[:, 0], ground[:, 1]) > LEVEL_MS
    if np.count_nonzero(moving) < MIN_MOVING_SAMPLES:
        return None
    n = int(np.count_nonzero(moving))
    x = np.zeros((2 * n, 3))
    x[0::2, 0] = 1.0
    x[1::2, 1] = 1.0
    x[0::2, 2] = ground[moving, 0]
    x[1::2, 2] = ground[moving, 1]
    coef = np.linalg.lstsq(x, air[moving].reshape(-1), rcond=None)[0]
    k = float(coef[2])
    if not K_RANGE[0] <= k <= K_RANGE[1]:
        return None
    return k, -coef[:2] / k


def load(text: str) -> dict[str, Array]:
    rows = list(csv.DictReader(io.StringIO(text)))
    return {name: np.array([float(r[name]) for r in rows]) for name in rows[0]}


def windows(a: dict[str, Array], wind: Array) -> list[dict[str, float | str]]:
    t = a["time"]
    q = np.stack([a[f"orientation_{c}"] for c in "xyzw"], -1)
    v = rotate(q, np.stack([a[f"velocity_{c}"] for c in "xyz"], -1))
    height = a["position_z"] - a["position_z"][0]
    power = a["battery_voltage"] * a["battery_current"]
    out: list[dict[str, float | str]] = []
    edges = np.arange(t[0], t[-1], TILE_S)
    for start in edges:
        m = (t >= start) & (t < start + TILE_S)
        if np.count_nonzero(m) < MIN_SAMPLES:
            continue
        tt, vv = t[m], v[m]
        seconds = float(tt[-1] - tt[0])
        if seconds <= 0:
            continue
        ground = vv[:, :2].mean(axis=0)
        speed = float(np.hypot(*ground))
        climb = float(vv[:, 2].mean())
        accel = float(np.hypot(*(vv[-1, :2] - vv[0, :2]))) / seconds
        track = np.unwrap(np.arctan2(vv[:, 0], vv[:, 1]))
        turn = abs(math.degrees(float(track[-1] - track[0]))) / seconds
        if float(height[m].mean()) < MIN_HEIGHT_M:
            continue
        if abs(climb) >= CLIMB_MS:
            phase = "climb" if climb > 0 else "descent"
        elif speed < HOVER_MS:
            phase = "hover"
        elif (speed >= LEVEL_MS and abs(climb) < LEVEL_CLIMB_MS and accel < MAX_ACCEL
              and turn < MAX_TURN_DEG_S):  # fmt: skip
            phase = "level"
        else:
            continue
        p = power[m]
        if float(np.std(p)) > MAX_POWER_CV * abs(float(np.mean(p))):
            continue
        air = vv[:, :2] - wind
        out.append({
            "phase": phase,
            "seconds": round(seconds, 2),
            "airspeed": round(float(np.hypot(*air.mean(axis=0))), 2),
            "anemometer": round(float(a["wind_speed"][m].mean()), 2),
            "ground": round(speed, 2),
            "climb": round(climb, 2),
            "power": round(float(p.mean()), 1),
            "volts": round(float(a["battery_voltage"][m].mean()), 2),
        })  # fmt: skip
    return out


def main(parameters: Path, flights: Path, out: Path) -> None:
    with parameters.open(newline="") as f:
        plan = {int(r["flight"]): r for r in csv.DictReader(f)}
    data: dict[int, dict[str, Array]] = {}
    with zipfile.ZipFile(flights) as z:
        for name in z.namelist():
            stem = Path(name).stem
            route = plan[int(stem)]["route"] if stem.isdigit() and int(stem) in plan else ""
            if name.endswith(".csv") and route.startswith(("R", "H")):
                data[int(stem)] = load(z.read(name).decode())
    air: dict[int, Array] = {}
    ground: dict[int, Array] = {}
    fits: dict[int, tuple[float, Array] | None] = {}
    for n, a in data.items():
        q = np.stack([a[f"orientation_{c}"] for c in "xyzw"], -1)
        ground[n] = rotate(q, np.stack([a[f"velocity_{c}"] for c in "xyz"], -1))[:, :2]
        bearing = np.radians(a["wind_angle"])
        air[n] = np.stack([a["wind_speed"] * np.sin(bearing),
                           a["wind_speed"] * np.cos(bearing)], -1)  # fmt: skip
        fits[n] = wind_fit(air[n], ground[n])
    k_median = float(np.median([f[0] for f in fits.values() if f is not None]))
    density = round(isa_density(float(np.median([a["position_z"][0] for a in data.values()]))), 2)
    rows: list[dict[str, float | str]] = []
    for n in sorted(data):
        fitted = fits[n]
        if fitted is None:
            still = np.hypot(ground[n][:, 0], ground[n][:, 1]) <= HOVER_MS
            wind = -np.mean(air[n][still], axis=0) / k_median if still.any() else np.zeros(2)
            source = "median k"
        else:
            wind = fitted[1]
            source = "own fit"
        payload = float(plan[n]["payload"])
        for w in windows(data[n], wind):
            rows.append({"flight": f"m100-{n}", "payload_g": payload,
                         "mass_kg": round(TAKEOFF_KG + payload / 1000.0, 3),
                         "speed_set_ms": float(plan[n]["speed"]), "wind_from": source,
                         "density": density, **w})  # fmt: skip
    fields = ["flight", "payload_g", "mass_kg", "speed_set_ms", "wind_from", "density", "phase",
              "seconds", "airspeed", "anemometer", "ground", "climb", "power", "volts"]  # fmt: skip
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} windows from {len(data)} flights; median k {k_median:.3f}; "
          f"density {density}")  # fmt: skip


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
