"""The power model on a real airframe: the CMU DJI Matrice 100 data set (Rodrigues et al.,
Scientific Data 8, 155, 2021; CC BY 4.0), 196 flights at 4-12 m/s with 0, 250 and 500 g,
airspeed from an onboard anemometer (tests/fixtures/public/cmu-m100/README.md).

The windows are binned into points as the engine bins a flight's tiles, and fitted as an
airframe's flights are. The model must hold the measured power at every speed and payload
flown, put the least power where the flights found it, and predict each flight's energy
from a model fitted without it within the validation targets (median 5%, 90th percentile
10%; spec: Power and efficiency analysis, "Range, endurance and speed", "Verification")."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from flight_insights.analyzers import Registry
from flight_insights.knowledge import load_pack
from flight_insights.performance.fit import G, Model, Row, energy_error, fit, level
from flight_insights.performance.optimize import best

RULES: dict[str, Any] = dict(Registry.load(load_pack()).rules["performance"])
FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/public/cmu-m100/windows.csv"
DISC = 4 * math.pi * (13 * 0.0254 / 2) ** 2  # four 13-inch propellers, none overlapping
COLUMNS = ["flight", "payload_g", "mass_kg", "speed_set_ms", "wind_from", "density", "phase",
           "seconds", "airspeed", "anemometer", "ground", "climb", "power", "volts"]  # fmt: skip
PAYLOADS = ("0.0", "250.0", "500.0")  # the payloads flown at every speed
SPAN = (3, 12)  # m/s: the airspeeds flown at every payload
MIN_SECONDS = 60.0  # in a bin before it is compared
FOLDS = 10

Window = dict[str, str]


@pytest.fixture(scope="module")
def windows() -> list[Window]:
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def points(windows: Sequence[Window]) -> list[Row]:
    """Each flight's windows averaged by phase, airspeed bin and climb bin, weighted by
    time, as `performance.points.tile_points` averages a flight's tiles."""
    spec = RULES["points"]
    groups: dict[tuple[str, str, int, int], list[Window]] = defaultdict(list)
    for w in windows:
        if w["phase"] in spec["phases"]:
            key = (w["flight"], w["phase"],
                   round(float(w["airspeed"]) / float(spec["airspeed_bin_ms"])),
                   round(float(w["climb"]) / float(spec["climb_bin_ms"])))  # fmt: skip
            groups[key].append(w)
    rows: list[Row] = []
    for (flight, *_), items in sorted(groups.items()):
        s = np.array([float(w["seconds"]) for w in items])

        def mean(name: str, items: list[Window] = items, s: np.ndarray[Any, Any] = s) -> float:
            return float(np.average([float(w[name]) for w in items], weights=s))

        rows.append(Row(flight, float(items[0]["mass_kg"]) * G, mean("airspeed"), mean("climb"),
                        mean("power"), float(items[0]["density"]), float(s.sum())))  # fmt: skip
    return rows


@pytest.fixture(scope="module")
def rows(windows: list[Window]) -> list[Row]:
    return points(windows)


@pytest.fixture(scope="module")
def model(rows: list[Row]) -> Model:
    m = fit(rows, DISC, False, RULES)
    assert m is not None
    return m


def by_bin(windows: Sequence[Window], model: Model, phase: str
           ) -> dict[tuple[str, int], tuple[float, float, float]]:  # fmt: skip
    """(payload, airspeed bin): seconds, measured and modelled mean power."""
    acc: dict[tuple[str, int], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for w in windows:
        if w["phase"] != phase:
            continue
        s = float(w["seconds"])
        p = model.power(float(w["mass_kg"]) * G, float(w["airspeed"]), float(w["climb"]),
                        float(w["density"]), RULES)  # fmt: skip
        a = acc[(w["payload_g"], round(float(w["airspeed"])))]
        a[0] += s
        a[1] += s * float(w["power"])
        a[2] += s * float(p)
    return {k: (s, po / s, pm / s) for k, (s, po, pm) in acc.items()}


def test_the_fixture_holds_no_position(windows: list[Window]) -> None:
    with FIXTURE.open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == COLUMNS
    assert len({w["flight"] for w in windows}) == 196
    assert {w["phase"] for w in windows} == {"hover", "level", "climb", "descent"}


def test_the_flights_support_the_full_model(rows: list[Row]) -> None:
    # level flight over a span of airspeeds at three weights
    assert level(rows, RULES) == "A"


def test_level_power_at_every_speed_and_payload(windows: list[Window], model: Model) -> None:
    compared = 0
    for (payload, speed), (s, measured, modelled) in by_bin(windows, model, "level").items():
        if payload in PAYLOADS and SPAN[0] <= speed <= SPAN[1] and s >= MIN_SECONDS:
            assert modelled == pytest.approx(measured, rel=0.05), (payload, speed)
            compared += 1
    assert compared >= 25


def test_hover_power_and_what_payload_costs(windows: list[Window], model: Model) -> None:
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for (payload, _), (s, measured, modelled) in by_bin(windows, model, "hover").items():
        if payload in PAYLOADS:
            a = sums[payload]
            a[0] += s
            a[1] += s * measured
            a[2] += s * modelled
    assert set(sums) == set(PAYLOADS)
    hover = {p: (m / s, pm / s) for p, (s, m, pm) in sums.items()}  # measured, modelled
    for measured, modelled in hover.values():
        assert modelled == pytest.approx(measured, rel=0.05)
    # 500 g costs what momentum theory and the flights say it costs
    measured_ratio = hover["500.0"][0] / hover["0.0"][0]
    modelled_ratio = hover["500.0"][1] / hover["0.0"][1]
    assert modelled_ratio == pytest.approx(measured_ratio, rel=0.02)


def test_least_power_where_the_flights_found_it(windows: list[Window], model: Model) -> None:
    bins = by_bin(windows, model, "level")
    for payload in PAYLOADS:
        flown = {v: m for (p, v), (s, m, _) in bins.items()
                 if p == payload and SPAN[0] <= v <= SPAN[1] and s >= MIN_SECONDS}  # fmt: skip
        least = min(flown, key=lambda v: flown[v])
        kg = float(next(w["mass_kg"] for w in windows if w["payload_g"] == payload))
        speeds = best(model, kg * G, float(windows[0]["density"]), RULES, 16.0)
        # flown at the model's best-endurance speed, the aircraft drew within the flat band
        # of the least the flights measured
        at = flown[round(speeds.endurance)]
        assert at <= flown[least] * (1 + float(RULES["speeds"]["band_share"])), payload
        # power per metre still falls at the fastest speed flown, in the flights and the model
        top = max(flown)
        assert flown[top] / top < min(flown[v] / v for v in flown if v < top)
        assert speeds.range >= top


def test_each_flights_energy_from_the_others(rows: list[Row]) -> None:
    """Out of sample, by flight: the flights in ten folds, each fold's flights predicted by
    the model fitted to the other nine (the leave-one-out the app runs, grouped to keep
    the test quick)."""
    names = sorted({r.flight for r in rows})
    errors: list[float] = []
    for k in range(FOLDS):
        held = set(names[k::FOLDS])
        m = fit([r for r in rows if r.flight not in held], DISC, False, RULES)
        assert m is not None
        for name in held:
            errors.append(energy_error(m, [r for r in rows if r.flight == name], RULES))
    e = np.abs(np.array(errors))
    assert float(np.median(e)) <= 0.05
    assert float(np.percentile(e, 90)) <= 0.10
