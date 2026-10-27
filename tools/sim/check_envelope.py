#!/usr/bin/env python3
"""The wind envelope against the simulator (spec: Simulator checks; Weather and wind,
"Measures the wind envelope"): ArduPilot's default quad holding position in GUIDED in a
steady wind just under and just over the strongest wind the app says it can hold.

The app's envelope comes from the wind analyzer run on the committed calm-air fixture
(copter-quad-wind-calm: legs at 3, 6 and 9 m/s on eight courses, which measure the
drag), with the case's lean limit. The quad then takes off to 20 m and holds position for
30 s in each wind (SIM_WIND_T 1: the same at every height; no turbulence):
- **under the envelope** (`margin` below it) it must hold: within 3 m of the target and
  not drifting;
- **over the envelope** (`margin` above it) it must not: leaning at its limit, it is
  carried off downwind.

Positions are read only as metres from the target in the estimator's local frame; nothing
reads or prints a latitude, longitude or absolute altitude. Run from engine/, which has
the app's code: uv run python ../tools/sim/check_envelope.py --ardupilot DIR [--keep DIR].
Parameters are only ever written to the local simulator this script starts.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.wind import WIND
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.platforms.records import create_airframe, create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import open_store
from flight_insights.store.keys import StoreKeys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fixtures" / "sitl"))
import scenarios as sc  # noqa: E402
from run_scenario import ScenarioFailed, Vehicle, sitl_command  # noqa: E402

PACK = load_pack()
REG = Registry.load(PACK)
CALM = ROOT / "tests/fixtures/sitl/4.7.0/copter-quad-wind-calm.bin"
ALT_M = 20.0
HOLD_S = 30.0
POSITION_ONLY = 0b110111111000  # SET_POSITION_TARGET_LOCAL_NED: use x, y, z


@dataclass(frozen=True)
class Case:
    name: str
    params: dict[str, float]  # the lean limit
    margin: float = 0.06  # how far under and over the envelope it flies


CASES = [
    Case("lean-30", {"ATC_ANGLE_MAX": 30.0}),
    Case("lean-45", {"ATC_ANGLE_MAX": 45.0}),
]


def predicted(params: dict[str, float]) -> tuple[float, str]:
    """The app's envelope for the calm fixture's drag with these lean settings, and what
    limits it."""
    keys = StoreKeys.generate()
    with tempfile.TemporaryDirectory() as tmp:
        store = open_store(Path(tmp) / "e.db", keys, "e" * 32)
        cache = ColumnCache(Path(tmp) / "cache", keys)
        af = create_airframe(store, create_platform(store, load_schema(PACK), "SITL", "copter"),
                             "SITL quad")  # fmt: skip
        batch = scan(store, PACK, bytes(32), [CALM])
        [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                       [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
        flight = FlightData.load(store, cache, PACK, res.flights[0])
        flight = dataclasses.replace(flight, params={**flight.params, **params})
        r = WIND.run(flight, REG)
        store.close()
    [hold] = [m.value for m in r.metrics if m.metric == "wind.envelope_ms"]
    [ev] = [e for e in r.evidence if e.id == "wind:envelope"]
    return hold, str(ev.values["limit_by"])


def held(v: Vehicle, seconds: float) -> tuple[float, float]:
    """Hold the take-off point for `seconds` of simulated time: the mean distance from it
    over the last third, and how fast that distance grew there (m/s)."""
    rows: list[tuple[float, float]] = []
    start = sent = None
    while True:
        m = v.conn.recv_match(type="LOCAL_POSITION_NED", blocking=True, timeout=10)
        if m is None:
            raise ScenarioFailed("no LOCAL_POSITION_NED messages")
        t = float(m.time_boot_ms) / 1000  # type: ignore[attr-defined]
        start = t if start is None else start
        if sent is None or t - sent >= 0.5:
            v.conn.mav.set_position_target_local_ned_send(
                0, v.sys, v.comp, sc.ML.MAV_FRAME_LOCAL_NED, POSITION_ONLY,
                0, 0, -ALT_M, 0, 0, 0, 0, 0, 0, 0, 0,
            )  # fmt: skip
            sent = t
        rows.append((t, math.hypot(m.x, m.y)))  # type: ignore[attr-defined]
        if t - start >= seconds:
            break
    tail = [(t, d) for t, d in rows if t >= start + seconds * 2 / 3]
    mt = sum(t for t, _ in tail) / len(tail)
    md = sum(d for _, d in tail) / len(tail)
    slope = sum((t - mt) * (d - md) for t, d in tail) / max(
        1e-9, sum((t - mt) ** 2 for t, _ in tail)
    )
    return md, slope


def fly(case: Case, winds: tuple[float, float], ardupilot: Path, work: Path
        ) -> list[tuple[float, float, float]]:  # fmt: skip
    """Each wind with how far off the quad was and how fast it drifted."""
    scenario = sc.Scenario(
        name=case.name, vehicle="copter", frame="quad +", description="wind envelope",
        expected=[], fly=lambda _v: None, boot_params=case.params, **sc.COPTER,  # type: ignore[arg-type]
    )  # fmt: skip
    folder = work / case.name
    folder.mkdir(parents=True, exist_ok=True)
    out = (folder / "sitl.txt").open("w")
    proc = subprocess.Popen(sitl_command(scenario, ardupilot, folder), cwd=folder, stdout=out,
                            stderr=subprocess.STDOUT)  # fmt: skip
    v = Vehicle(folder / f"{case.name}.tlog")
    results: list[tuple[float, float, float]] = []
    try:
        v.wait_ready()
        if v.get_param("SIM_SPEEDUP") <= 0:  # only the simulator has SIM_ parameters
            raise ScenarioFailed("not a simulator")
        for name, value in {"SIM_WIND_SPD": 0, "SIM_WIND_DIR": 270, "SIM_WIND_T": 1,
                            "SIM_WIND_TURB": 0, **case.params}.items():  # fmt: skip
            v.set_param(name, value)
        v.mode("GUIDED")
        v.arm()
        v.command(sc.ML.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, ALT_M)
        v.wait_altitude(ALT_M - 0.5)
        held(v, 10)
        for wind in winds:
            v.set_param("SIM_WIND_SPD", wind)
            off, drift = held(v, HOLD_S)
            results.append((wind, off, drift))
            if off > 30:
                break
        v.set_param("SIM_WIND_SPD", 0)
        try:
            sc.land_and_disarm(v)
        except ScenarioFailed:
            v.disarm(force=True)
        v.close()
    except ScenarioFailed as e:
        raise ScenarioFailed(f"{case.name}: {e}; last messages {v.texts[-5:]}") from e
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        out.close()
    return results


def check(case: Case, ardupilot: Path, work: Path) -> list[str]:
    hold, limit = predicted(case.params)
    under, over = hold * (1 - case.margin), hold * (1 + case.margin)
    flown = fly(case, (under, over), ardupilot, work)
    problems: list[str] = []
    for wind, off, drift in flown:
        holds = off < 3.0 and drift < 0.1
        want = wind < hold
        print(f"  {case.name}: envelope {hold:.2f} m/s ({limit}); {wind:.2f} m/s: {off:.1f} m off,"
              f" drifting {drift:+.2f} m/s -> {'holds' if holds else 'carried off'}")  # fmt: skip
        if holds != want:
            problems.append(
                f"{case.name}: at {wind:.2f} m/s, {'under' if want else 'over'} the app's"
                f" envelope of {hold:.2f}, the quad {'was carried off' if want else 'held'}"
            )
    if len(flown) < 2:
        problems.append(f"{case.name}: flew {len(flown)} of the 2 winds")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--ardupilot", type=Path, required=True)
    parser.add_argument("--keep", type=Path, help="keep the logs in this folder")
    parser.add_argument("cases", nargs="*", help="the cases to fly (all by default)")
    args = parser.parse_args(argv)
    chosen = [c for c in CASES if not args.cases or c.name in args.cases]
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = args.keep or Path(tmp)
        for case in chosen:
            try:
                problems += check(case, args.ardupilot, work)
            except ScenarioFailed as e:
                print(f"  FAIL  {e}")
                problems.append(str(e))
    print(json.dumps({"problems": problems}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
