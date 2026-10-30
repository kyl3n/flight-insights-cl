"""The local validation runner (tools/props/validate.py) on made-up tables: one motor of
known constants on three propellers of one family whose theory is known. Run from the
engine's environment: cd engine && uv run pytest ../tools/props/tests."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flight_insights.performance.motor import RPM_TO_RAD, Motor
from flight_insights.performance.propeller import INCH, G
from flight_insights.performance.propfamily import Prop, coefficients
from validate import main, validate

TRUE = np.array([5.0, -4.5, 0.012, 0.9, 0.35])  # the made-up family's theory
MOTOR = Motor(kv=170.0, r0=0.06, r2=2e-6, i00=0.6, i01=0.002)
VOLTS, RHO, AMBIENT = 48.0, 1.2, 20.0


def write(folder: Path, prop: Prop, name: str, offset: float = 0.0) -> None:
    """A table the motor and propeller give, the propeller's power `offset` (log) from
    its family's theory."""
    from flight_insights.analyzers import Registry
    from flight_insights.knowledge import load_pack
    from flight_insights.performance.bench import test_density

    rules = Registry.load(load_pack()).rules["props"]
    rho = test_density(AMBIENT)
    d = prop.diameter_in * INCH

    def coeffs(omega: float) -> tuple[float, float]:
        ct, cp = coefficients(TRUE, rules, prop, max(omega, 1.0) / RPM_TO_RAD, rho)
        return float(ct[0]), float(cp[0]) * math.exp(offset)

    def torque(omega: Any) -> float:
        n = float(omega) / (2 * math.pi)
        return coeffs(float(omega))[1] * rho * n**2 * d**5 / (2 * math.pi)

    rows = []
    for pct in (40, 50, 60, 70, 80, 90, 100):
        omega = MOTOR.top_speed(torque, VOLTS, pct / 100 * 0.98)
        n = omega / (2 * math.pi)
        ct, _ = coeffs(omega)
        q = torque(omega)
        p = float(MOTOR.power(q, omega))
        rows.append({"throttle_pct": pct, "thrust_g": ct * rho * n**2 * d**4 / G * 1000,
                     "power_w": p, "rpm": omega / RPM_TO_RAD, "torque_nm": q,
                     "current_a": p / VOLTS, "volts": VOLTS})  # fmt: skip
    table = {"motor": "made-up", "family": prop.family, "diameter_in": prop.diameter_in,
             "pitch_in": prop.pitch_in, "blades": 2, "kv": 170, "volts": VOLTS,
             "ambient_c": AMBIENT, "rows": rows}  # fmt: skip
    (folder / f"{name}.json").write_text(json.dumps(table), encoding="utf-8")


def corpus(folder: Path, offsets: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Path:
    for (d, p), off in zip(((20.0, 6.0), (22.0, 6.6), (24.0, 7.6)), offsets, strict=True):
        write(folder, Prop("tmotor_p", d, p), f"t{d:g}", off)
    return folder


def test_the_family_true_meets_the_targets(tmp_path: Path) -> None:
    got = validate(sorted(corpus(tmp_path).glob("*.json")))
    assert got["tables"] == 3 and got["motors"] == 1 and got["pairs"] == 6
    assert got["steps"] > 10
    assert got["hover"]["median"] < 0.02 and got["hover"]["largest"] < 0.05
    assert got["most_thrust"]["largest"] < 0.03 and got["meets_targets"]
    assert not got["enough"]  # one motor's three propellers do not make a corpus


def test_propellers_far_from_their_theory_fail(tmp_path: Path) -> None:
    got = validate(sorted(corpus(tmp_path, (0.0, 0.25, -0.25)).glob("*.json")))
    assert not got["meets_targets"] and got["hover"]["p90"] > 0.10


def test_the_command_prints_aggregates_only(tmp_path: Path, capsys: Any) -> None:
    folder = corpus(tmp_path)
    out = tmp_path / "result.json"
    assert main([str(folder), "--out", str(out)]) == 1  # accurate, but too little data
    printed = capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(printed)
    assert "t22" not in printed and "made-up" not in printed and "tmotor" not in printed
    assert main([str(tmp_path / "empty")]) == 2
