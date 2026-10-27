#!/usr/bin/env python3
"""The simulator checks (spec: Simulator checks): export airframe models from the
committed SITL fixtures, fly each with the bundle's own flight script
(knowledge/sim/fly_sitl.py) in ArduPilot's simulator, and compare what it flew with what
the app predicted.

- **Round trip.** The default quad (from its wind-measuring legs) and Callisto (from its
  light hover) are exported, flown again on the same profiles, and exported again from
  those flights: the hover and power must come back within 0.5% and 1%, and the quad's
  drag at the legs' speeds within 3%.
- **Losing a motor at the heaviest take-off.** Callisto exported at 40.5 kg (the loaded
  fixture's weight) hovers while each motor is stopped in turn. In each loss the busiest
  remaining motor's output must be within 3 points of what the app's motor-out model of
  ArduPilot's mixer predicts from the same flight's hover just before, the aircraft must
  hold its height within 1 m and its heading within 15 degrees.

Run from engine/, which has the app's code: uv run python ../tools/sim/check_sim.py
--ardupilot DIR [--keep DIR]. Parameters are only ever written to the local simulator,
which the flight script starts and checks itself.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import tempfile
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
from flight_insights.analyzers.run import run_analysis
from flight_insights.dataflash import read_dataflash
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.motor_out import output_for, worst
from flight_insights.platforms.records import (
    create_airframe,
    create_platform,
    platform_view,
    save_answers,
)
from flight_insights.platforms.schema import load_schema
from flight_insights.sim import export as sx
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "sitl" / "4.7.0"
PACK = load_pack()
QUAD = {"wheelbase_mm": 350, "prop_diameter_in": 13.78}  # ArduPilot's default quad's size
CALLISTO = {"wheelbase_mm": 1325, "prop_diameter_in": 30}  # Callisto.json
HEAVIEST_KG = 40.5
G = 9.80665
Array = np.ndarray[Any, Any]


def flight_script() -> ModuleType:
    path = ROOT / "knowledge" / "sim" / "fly_sitl.py"
    spec = importlib.util.spec_from_file_location("fly_sitl", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fleet:
    def __init__(self, folder: Path) -> None:
        keys = StoreKeys.generate()
        self.store: Store = open_store(folder / "sim.db", keys, "e" * 32)
        self.cache = ColumnCache(folder / "cache", keys)
        self.schema = load_schema(PACK)

    def airframe(self, name: str, answers: dict[str, Any]) -> str:
        platform = create_platform(self.store, self.schema, name, "copter")
        version = platform_view(self.store, self.schema, platform)["reference_build"]["version"]
        save_answers(self.store, self.schema, platform, version, answers)
        return create_airframe(self.store, platform, name)

    def flown(self, log: Path, airframe: str, kg: float) -> str:
        batch = scan(self.store, PACK, bytes(32), [log])
        [res] = commit(self.store, PACK, self.cache, batch, propose(self.store, PACK, batch),
                       [Decision(batch[0].id, airframe_id=airframe, takeoff_kg=kg)])  # fmt: skip
        flight = res.flights[0]
        run_analysis(self.store, self.cache, PACK, flight)
        return flight

    def bundle(self, airframe: str, folder: Path, kg: float | None = None) -> sx.Export:
        ex = sx.export(self.store, self.cache, PACK, airframe, takeoff_kg=kg)
        folder.mkdir(parents=True, exist_ok=True)
        for f in sx.files(ex, PACK):
            (folder / f["filename"]).write_text(f["text"], encoding="utf-8")
        return ex


def _close(name: str, got: float, want: float, rel: float) -> list[str]:
    off = abs(got - want) / abs(want)
    line = f"{name}: {got:.6g} against {want:.6g} ({100 * off:.2f}%, within {100 * rel:g}%)"
    print(("  ok    " if off <= rel else "  FAIL  ") + line)
    return [] if off <= rel else [line]


def _drag(model: dict[str, float], speed: float) -> float:
    """The simulator's drag on the model, per unit mass, in still sea-level air at this
    airspeed: k1 v + k2 v^2, with k1 = mdrag_coef sqrt(g rho A / m) and k2 = areaCd rho / 2m."""
    rho = sx.isa_density(model["refAlt"])
    area, mdrag = sx.area_cd(model, G)
    k1 = mdrag * math.sqrt(G * rho * model["disc_area"] / model["mass"])
    k2 = area * rho / (2.0 * model["mass"])
    return k1 * speed + k2 * speed * speed


def round_trip(fleet: Fleet, fly: Callable[..., Path], ardupilot: Path, work: Path,
               name: str, fixture: str, answers: dict[str, Any], kg: float, flight: str,
               speeds: tuple[float, ...] = ()) -> tuple[list[str], str]:  # fmt: skip
    """Problems found, and the airframe flown from the fixture."""
    print(f"round trip: {name}")
    first = fleet.airframe(f"{name} flown", answers)
    fleet.flown(FIXTURES / f"{fixture}.bin", first, kg)
    ex = fleet.bundle(first, work / name)
    stem = sx.stem(ex)
    log = fly(ardupilot, work / name / f"{stem}.json", ex.sitl, work / name / f"{stem}.param",
              flight, work / f"{name}-{flight}.bin")  # fmt: skip
    again = fleet.airframe(f"{name} simulated", answers)
    fleet.flown(log, again, kg)
    back = fleet.bundle(again, work / f"{name}-again")
    a, b = ex.model, back.model
    problems = _close("hoverThrOut", b["hoverThrOut"], a["hoverThrOut"], 0.005)
    problems += _close("hover power", b["refVoltage"] * b["refCurrent"],
                       a["refVoltage"] * a["refCurrent"], 0.01)  # fmt: skip
    if speeds:
        # The drag the simulator flies each model with, at the legs' speeds. The fit can
        # trade its momentum term against its bluff-body term within its own intervals
        # (each about 2.5% on these flights), so the split is reported, not checked.
        for speed in speeds:
            problems += _close(f"drag at {speed:g} m/s", _drag(b, speed), _drag(a, speed), 0.03)
        for name, x, y in (("mdrag_coef", b["mdrag_coef"], a["mdrag_coef"]),
                           ("areaCd", sx.area_cd(b, G)[0], sx.area_cd(a, G)[0])):  # fmt: skip
            print(f"  info  {name}: {x:.6g} against {y:.6g} ({100 * (x / y - 1):+.2f}%)")
    return problems, first


def _column(table: Any, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(), float)


def motor_losses(log: Path, ex: sx.Export) -> list[str]:
    """Each stopped motor's window against the mixer model, from the hover before it."""
    data = read_dataflash(log)
    parm = data.table("PARM")
    names = [n.rstrip(b"\x00").decode() for n in parm.column("Name").to_pylist()]
    values = _column(parm, "Value")
    times = _column(parm, "TimeUS")
    changes: list[tuple[float, int]] = []
    for t, n, v in zip(times, names, values, strict=True):
        if n == "SIM_ENGINE_FAIL" and (not changes or changes[-1][1] != int(v)):
            changes.append((float(t), int(v)))
    windows = [(a, b, mask.bit_length()) for (a, mask), (b, after) in pairwise(changes)
               if mask and not after]  # fmt: skip
    p = ex.params
    count = int(ex.model["num_motors"])
    rcou = data.table("RCOU")
    t = _column(rcou, "TimeUS")
    low, high = p["MOT_PWM_MIN"], p["MOT_PWM_MAX"]
    spin_min, spin_max = ex.model["spin_min"], ex.model["spin_max"]
    pwm = np.column_stack([_column(rcou, f"C{k}") for k in range(1, count + 1)])
    out = ((pwm - low) / (high - low) - spin_min) / (spin_max - spin_min)
    bat = data.table("BAT")
    volts = np.interp(t, _column(bat, "TimeUS"), _column(bat, "Volt"))
    ctun, att = data.table("CTUN"), data.table("ATT")
    alt = np.interp(t, _column(ctun, "TimeUS"), _column(ctun, "Alt"))
    ta = _column(att, "TimeUS")
    yaw = np.unwrap(np.radians(np.interp(t, ta, _column(att, "Yaw"))))
    tilt = (np.cos(np.radians(np.interp(t, ta, _column(att, "Roll"))))
            * np.cos(np.radians(np.interp(t, ta, _column(att, "Pitch")))))  # fmt: skip
    expo = ex.model["propExpo"]
    volt_ref = float(p.get("MOT_BAT_VOLT_MAX") or ex.model["maxVoltage"])
    frame = PACK.frames(ex.firmware)[str(int(p["FRAME_CLASS"]))]["types"]
    motors = (frame.get(str(int(p["FRAME_TYPE"]))) or frame["default"])["motors"]
    print(f"losing each motor: {ex.frame} at {ex.model['mass']:g} kg ({len(windows)} losses)")
    problems: list[str] = [] if len(windows) == count else [f"{len(windows)} losses flown"]
    for start, end, motor in windows:
        before = (t > start - 6e6) & (t < start - 1e6)
        x = out[before] * (volts[before] / volt_ref)[:, None]
        hover = float(np.median(((1 - expo) * x + expo * x * x).mean(axis=1) * tilt[before]))
        lost = {m.failed: m for m in worst(motors, hover * count)}[motor]
        during = (t > end - 6e6) & (t < end - 0.5e6)
        others = [i for i in range(count) if i != motor - 1]
        busiest = float(np.median(out[during][:, others].max(axis=1)))
        if lost.peak is None:
            problems.append(f"motor {motor}: the model finds no balance")
            continue
        predicted = output_for(lost.peak, expo) * volt_ref / float(np.median(volts[during]))
        drop = float(np.median(alt[before]) - alt[(t > start) & (t < end)].min())
        turn = float(np.degrees(np.ptp(yaw[(t > start) & (t < end)])))
        ok = abs(busiest - predicted) <= 0.03 and drop <= 1.0 and turn <= 15.0
        line = (f"motor {motor}: busiest {100 * busiest:.1f}% against {100 * predicted:.1f}% "
                f"predicted; height lost {drop:.2f} m; heading moved {turn:.1f} deg")  # fmt: skip
        print(("  ok    " if ok else "  FAIL  ") + line)
        if not ok:
            problems.append(line)
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--ardupilot", type=Path, required=True)
    parser.add_argument("--keep", type=Path, help="keep the bundles and logs in this folder")
    args = parser.parse_args(argv)
    script = flight_script()
    fly = script.fly
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = args.keep or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        fleet = Fleet(Path(tmp))
        quad = ("quad", "copter-quad-wind-calm", QUAD, 3.0, "legs")
        callisto = ("callisto", "copter-heavylift-light", CALLISTO, 32.5, "hover")
        problems += round_trip(fleet, fly, args.ardupilot, work, *quad, script.LEG_SPEEDS)[0]
        found, light = round_trip(fleet, fly, args.ardupilot, work, *callisto)
        problems += found
        # The light flight's airframe again, exported at the heaviest take-off.
        folder = work / "heaviest"
        ex = fleet.bundle(light, folder, HEAVIEST_KG)
        stem = sx.stem(ex)
        log = fly(args.ardupilot, folder / f"{stem}.json", ex.sitl, folder / f"{stem}.param",
                  "motors", work / "heaviest-motors.bin")  # fmt: skip
        problems += motor_losses(log, ex)
        fleet.store.conn.close()
    print(json.dumps({"problems": problems}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
