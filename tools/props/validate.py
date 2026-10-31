"""Local validation of the propeller and motor predictions (spec: Power and efficiency
analysis, "Propeller and motor choice", "Validation before advice is given").

    cd engine && uv run python ../tools/props/validate.py CORPUS_DIR [--out RESULT_JSON]

CORPUS_DIR holds makers' tables a person saved on their own laptop; they are never
committed. One JSON file per table, in the bench table's own shape:

    {"motor": "...", "family": "tmotor_p", "diameter_in": 22, "pitch_in": 6.6,
     "blades": 2, "kv": 170, "volts": 48, "ambient_c": 34.5,
     "rows": [{"throttle_pct": 40, "thrust_g": 2067, "power_w": 206, "rpm": 3265,
               "torque_nm": 0.52, "current_a": 4.3, "volts": 47.92}, ...]}

Each motor's tables on two or more propellers are read pair by pair, each propeller
predicted from each other one as the app predicts a candidate from the propeller flown
(leave-one-propeller-out):

- the family calibrated on every table but the predicted propeller's;
- the motor fitted on its other tables (none: the shaft power is compared instead);
- the anchor: the other propeller's step nearest each thrust, as a hover measured on an
  aircraft would be; only thrusts within the other table's are read;
- the predicted electrical power at each step's thrust against the table's, and with
  its spread, whether the table lies inside the 95% interval;
- the most thrust at the predicted table's top step's voltage against that step, where
  the table reaches full throttle with its voltage and a motor model is fitted.

A motor's tables at several voltages are each predicted from each of its other
propellers' (`table_pairs`); `pairs` counts the propellers, each predicted from each other
one on the same motor, as the app's Propellers view counts the bench tables entered.

Prints aggregates only: the counts, the hover power error's median, 90th percentile and
largest, the most thrust's error, the interval's coverage, and each against the plan's
targets (5%, 10%, 8%), and whether the corpus is big enough to count (the rules'
`advice.least_pairs` pairs on `advice.least_motors` motors). No table, motor or
propeller is named in the output. A passing run on enough data sets
`advice.validated_error` in knowledge/rules/props.json from its 90th percentile; the exit
status is 0 only then.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.bench import Bench, Row, test_density
from flight_insights.performance.candidate import Basis, most_thrust
from flight_insights.performance.motor import RPM_TO_RAD, Motor, from_tables
from flight_insights.performance.propeller import RHO_STANDARD, G, table_points
from flight_insights.performance.propfamily import Operating, Prop, calibrate, predict

TARGETS = {"hover_median": 0.05, "hover_p90": 0.10, "most_thrust": 0.08}
EXTRA = ("rpm", "torque_nm", "current_a", "volts")


def read(path: Path) -> tuple[Bench, Prop, str]:
    """A saved table, its propeller and its motor."""
    d: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    rows = tuple(Row(float(r["throttle_pct"]), float(r["thrust_g"]), float(r["power_w"]),
                     **{k: float(r[k]) for k in EXTRA if r.get(k) is not None})
                 for r in sorted(d["rows"], key=lambda r: float(r["throttle_pct"])))  # fmt: skip
    prop = Prop(str(d["family"]), float(d["diameter_in"]), float(d["pitch_in"]),
                int(d.get("blades", 2)))  # fmt: skip
    bench = Bench(path.stem, 1, "none", path.stem, d.get("motor"), d.get("kv"),
                  prop.diameter_in, prop.pitch_in, d.get("volts"), None, None, rows,
                  d.get("ambient_c"), prop.family, prop.blades)  # fmt: skip
    return bench, prop, str(d["motor"])


def _density(b: Bench) -> float:
    return test_density(b.ambient_c) if b.ambient_c is not None else RHO_STANDARD


def pair(held: tuple[Bench, Prop], other: tuple[Bench, Prop], tables: Sequence[tuple[Bench, Prop]],
         motor_tables: Sequence[Bench], props: Mapping[str, Any],
         perf: Mapping[str, Any]) -> dict[str, list[float]]:  # fmt: skip
    """`held` predicted from `other` (the same motor): each step's hover power error and
    its z, and the most thrust's error."""
    hb, hp = held
    ob, op = other
    family = calibrate(props, hp.family, [(p, table_points(b)[0]) for b, p in tables
                                          if p.key != hp.key])  # fmt: skip
    rest = [b.rows for b in motor_tables if b is not hb]
    kv = next((float(b.kv) for b in motor_tables if b.kv), None)
    motor: Motor | None = from_tables(rest, kv, perf) if kv else None
    anchors = [p for p in table_points(ob)[0] if p.cp is not None]
    out: dict[str, list[float]] = {"hover": [], "z": [], "most": []}
    if not anchors:
        return out
    lo, hi = min(a.thrust_n for a in anchors), max(a.thrust_n for a in anchors)
    rho = _density(hb)
    for r in hb.rows:
        thrust = r.thrust_g / 1000.0 * G
        if r.rpm is None or not lo <= thrust <= hi:
            continue
        a = min(anchors, key=lambda x: abs(math.log(x.thrust_n / thrust)))
        at = Operating(a.rpm, a.ct, a.cp, a.density)
        got = predict(props, {hp.family: family}, hp, thrust, rho, op, at)
        if got.shaft_w is None or got.power_spread is None:
            continue
        if motor is not None:
            omega = got.rpm * RPM_TO_RAD
            predicted, measured = float(motor.power(got.shaft_w / omega, omega)), r.power_w
        elif r.shaft_w is not None:
            predicted, measured = got.shaft_w, r.shaft_w
        else:
            continue
        error = math.log(predicted / measured)
        out["hover"].append(error)
        out["z"].append(error / got.power_spread)
    top = hb.rows[-1]
    if (
        motor is not None
        and top.throttle_pct >= float(perf["motor"]["full_throttle_pct"])
        and top.volts
    ):
        a = max(anchors, key=lambda x: x.thrust_n)
        anchor = Operating(a.rpm, a.ct, a.cp, a.density)
        basis = Basis(flown=op, families={hp.family: family}, motor=motor, at=anchor,
                      motors=1, lowest_v=top.volts, nominal_v=top.volts, arms=None,
                      wheelbase_mm=None)  # fmt: skip
        got_top = most_thrust(basis, props, hp, motor, rho, float(perf["motor"]["full_duty"]))
        if got_top is not None:
            out["most"].append(math.log(got_top["thrust_n"] / (top.thrust_g / 1000.0 * G)))
    return out


def validate(paths: Sequence[Path]) -> dict[str, Any]:
    """The aggregates over every pair of propellers on the same motor."""
    reg = Registry.load(load_pack())
    props, perf = reg.rules["props"], reg.rules["performance"]
    tables = [read(p) for p in sorted(paths)]
    by_motor: dict[str, list[tuple[Bench, Prop]]] = {}
    for b, p, m in tables:
        by_motor.setdefault(m, []).append((b, p))
    all_tables = [(b, p) for b, p, _ in tables]
    hover: list[float] = []
    z: list[float] = []
    most: list[float] = []
    table_pairs = 0
    pairs = 0  # each propeller predicted from each other one on the same motor
    for group in by_motor.values():
        n = len({p.key for _, p in group})
        if n < 2:
            continue
        pairs += n * (n - 1)
        for held in group:
            for other in group:
                if other[1].key == held[1].key:
                    continue
                got = pair(held, other, all_tables, [b for b, _ in group], props, perf)
                table_pairs += 1
                hover += got["hover"]
                z += got["z"]
                most += got["most"]
    out: dict[str, Any] = {"tables": len(tables), "motors": len(by_motor), "pairs": pairs,
                           "table_pairs": table_pairs, "steps": len(hover),
                           "targets": TARGETS}  # fmt: skip
    if hover:
        e = np.abs(np.expm1(np.array(hover)))  # as shares of the measured power
        out["hover"] = {"median": float(np.median(e)), "p90": float(np.percentile(e, 90)),
                        "largest": float(np.max(e)),
                        "inside_95": float(np.mean(np.abs(z) <= 1.96))}  # fmt: skip
    if most:
        m = np.abs(np.expm1(np.array(most)))
        out["most_thrust"] = {"median": float(np.median(m)), "largest": float(np.max(m)),
                              "pairs": len(most)}  # fmt: skip
    advice = props["advice"]
    out["enough"] = pairs >= int(advice["least_pairs"]) and len(
        [g for g in by_motor.values() if len({p.key for _, p in g}) >= 2]
    ) >= int(advice["least_motors"])
    out["meets_targets"] = bool(
        hover and out["hover"]["median"] <= TARGETS["hover_median"]
        and out["hover"]["p90"] <= TARGETS["hover_p90"]
        and (not most or out["most_thrust"]["largest"] <= TARGETS["most_thrust"])
    )  # fmt: skip
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local validation of the propeller predictions")
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    paths = sorted(args.corpus.glob("*.json"))
    if not paths:
        print(f"no tables (*.json) in {args.corpus}", file=sys.stderr)
        return 2
    got = validate(paths)
    text = json.dumps(got, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if got["meets_targets"] and got["enough"] else 1


if __name__ == "__main__":
    sys.exit(main())
