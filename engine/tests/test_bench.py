"""Makers' bench tables and the comparison of two builds (performance/bench.py). Every
table here is made up: power grows as thrust to the 1.5, so log-log reading between the
steps is exact and the ratio of two tables is known at any thrust."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_perf_store import PACK

from flight_insights.performance import bench
from flight_insights.performance.fit import Model
from flight_insights.platforms.records import create_platform
from flight_insights.platforms.schema import load_schema
from flight_insights.store import synced
from flight_insights.store.db import Store, StoreError, open_store
from flight_insights.store.keys import StoreKeys

THRUSTS = (1500.0, 3000.0, 5000.0, 7500.0, 10000.0, 12500.0)


def table(c: float, thrusts: tuple[float, ...] = THRUSTS, top: float = 100.0,
          **extra: Any) -> dict[str, Any]:  # fmt: skip
    """A made-up table: power = c * thrust^1.5, throttle evenly up to `top`."""
    n = len(thrusts)
    rows = [{"throttle_pct": top * (k + 1) / n, "thrust_g": t, "power_w": c * t**1.5}
            for k, t in enumerate(thrusts)]  # fmt: skip
    return {"label": f"made-up {c}", "prop_diameter_in": 22, "prop_pitch_in": 7,
            "volts": 48, "rows": rows} | extra  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return open_store(tmp_path / "b.db", StoreKeys.generate(), "e" * 32)


@pytest.fixture
def platform(store: Store) -> str:
    return create_platform(store, load_schema(PACK), "Heavy", "copter")


def test_power_is_read_between_the_steps_and_never_past_them(store: Store,
                                                              platform: str) -> None:  # fmt: skip
    b = bench.save(store, platform, table(0.002, role="current"))
    for t in (1500.0, 4000.0, 6200.0, 12500.0):
        assert b.power_at(t) == pytest.approx(0.002 * t**1.5, rel=1e-9)
    assert b.power_at(1499.0) is None and b.power_at(12600.0) is None
    assert b.max_thrust_g == 12500.0 and b.full_throttle
    v = b.view()
    assert v["role"] == "current" and len(v["rows"]) == 6 and v["volts"] == 48.0


@pytest.mark.parametrize(
    ("rows", "says"),
    [
        ([], "at least three"),
        ([{"throttle_pct": 50, "thrust_g": 1, "power_w": 1}] * 2, "at least three"),
        ([{"throttle_pct": 50, "thrust_g": 1}], "throttle %, thrust in g and power in W"),
        ([{"throttle_pct": 0, "thrust_g": 1, "power_w": 1}], "above 0 and up to 100"),
        ([{"throttle_pct": 120, "thrust_g": 1, "power_w": 1}], "above 0 and up to 100"),
        ([{"throttle_pct": 50, "thrust_g": -1, "power_w": 1}], "above zero"),
        ([{"throttle_pct": t, "thrust_g": g, "power_w": 10}
          for t, g in ((20, 1), (40, 2), (60, 3))], "rise with throttle"),
        ([{"throttle_pct": t, "thrust_g": g, "power_w": p}
          for t, g, p in ((20, 1, 1), (40, 3, 2), (60, 2, 3))], "rise with throttle"),
    ],
)  # fmt: skip
def test_a_table_is_checked(store: Store, platform: str, rows: list[Any], says: str) -> None:
    with pytest.raises(bench.BenchError, match=says):
        bench.save(store, platform, table(0.002) | {"rows": rows})


def test_the_rest_of_a_table_is_checked(store: Store, platform: str) -> None:
    with pytest.raises(bench.BenchError, match="label"):
        bench.save(store, platform, table(0.002) | {"label": "  "})
    with pytest.raises(bench.BenchError, match="role"):
        bench.save(store, platform, table(0.002, role="spare"))
    with pytest.raises(bench.BenchError, match="kv is a number above zero"):
        bench.save(store, platform, table(0.002, kv=-170))
    with pytest.raises(bench.BenchError, match="kv is a number above zero"):
        bench.save(store, platform, table(0.002, kv=True))
    with pytest.raises(bench.BenchError, match="air temperature"):
        bench.save(store, platform, table(0.002, ambient_c=80))
    with pytest.raises(bench.BenchError, match="no such platform"):
        bench.save(store, "nope", table(0.002))
    steps = table(0.002)["rows"]  # entered in any order, kept by throttle
    got = bench.save(store, platform, table(0.002, rows=steps[::-1], kv="", motor=" "))
    assert [r.throttle_pct for r in got.rows] == sorted(r["throttle_pct"] for r in steps)
    assert got.kv is None and got.motor is None


def test_one_table_per_role_and_changes_name_their_version(store: Store,
                                                           platform: str) -> None:  # fmt: skip
    a = bench.save(store, platform, table(0.002, role="current", label="A"))
    b = bench.save(store, platform, table(0.0018, role="candidate", label="B"))
    c = bench.save(store, platform, table(0.0019, role="current", label="C"))
    roles = {x.label: x.role for x in bench.benches(store, platform)}
    assert roles == {"A": "none", "B": "candidate", "C": "current"}
    assert [x.label for x in bench.benches(store, platform)][:2] == ["C", "B"]
    with pytest.raises(bench.BenchError, match="version"):
        bench.save(store, platform, table(0.002, label="A2"), a.id)
    with pytest.raises(StoreError):
        bench.save(store, platform, table(0.002, label="A2"), a.id, a.version - 1 or 99)
    a2 = bench.save(store, platform, table(0.002, label="A2", role="candidate"), a.id,
                    bench.benches(store, platform)[-1].version)  # fmt: skip
    roles = {x.id: x.role for x in bench.benches(store, platform)}
    assert roles == {a2.id: "candidate", b.id: "none", c.id: "current"}


def test_the_ratio_and_what_limits_reading_it(store: Store, platform: str) -> None:
    now = bench.save(store, platform, table(0.002))
    then = bench.save(store, platform, table(0.0018))
    c = bench.compare(now, then, 4062.5, 1.225)
    assert c["share"] == pytest.approx(0.9, rel=1e-9) and c["notes"] == []
    short = bench.save(store, platform, table(0.0016, (1500.0, 2500.0, 3500.0), top=62,
                                              prop_diameter_in=21, volts=44))  # fmt: skip
    c = bench.compare(now, short, 4062.5, 1.225)
    assert c["share"] is None and c["candidate_w"] is None
    assert c["notes"] == ["props_differ", "volts_differ", "candidate_out_of_range",
                          "candidate_short"]  # fmt: skip
    assert bench.compare(now, then, 1000.0, 1.225)["notes"] == ["current_out_of_range",
                                                         "candidate_out_of_range"]  # fmt: skip


def test_tables_are_read_in_the_same_air(store: Store, platform: str) -> None:
    """The same motor measured at 15 °C and at 35 °C: once each table is brought to the
    same air, they agree; a table without its temperature is read as measured."""
    cool = bench.save(store, platform, table(0.002, ambient_c=15))
    # the hot bench needs sqrt(rho_15 / rho_35) = sqrt(308.15 / 288.15) more for the thrust
    hot_c = 0.002 * math.sqrt(308.15 / 288.15)
    hot = bench.save(store, platform, table(hot_c, ambient_c=35))
    assert hot.ambient_c == 35.0 and hot.view()["ambient_c"] == 35.0
    c = bench.compare(cool, hot, 4000.0, 1.225)
    assert c["same_air"] and c["share"] == pytest.approx(1.0, rel=1e-9) and c["notes"] == []
    # in standard air at 15 °C the cool table needs no correction
    assert cool.power_in(4000.0, bench.test_density(15.0)) == pytest.approx(0.002 * 4000**1.5)
    plain = bench.save(store, platform, table(0.002))
    c = bench.compare(cool, plain, 4000.0, 1.225)
    assert not c["same_air"] and c["notes"] == ["ambient_unknown"]
    assert c["current_w"] == pytest.approx(0.002 * 4000**1.5)  # as measured, uncorrected
    assert bench.compare(plain, plain, 4000.0, 1.0)["notes"] == []


def test_only_the_propulsion_power_scales() -> None:
    m = Model(avionics=60.0, induced=1.0, flight=2.0, profile=3.0, growth=4.0,
              drag_area=0.5, disc_area=1.2, rms=0.03)  # fmt: skip
    got = bench.propulsion(m, 0.9)
    assert got == replace(m, induced=0.9, flight=1.8, profile=2.7)


def test_bench_tables_sync() -> None:
    assert "bench_table" in synced.RECORDS


def test_a_table_keeps_the_columns_the_maker_prints(store: Store, platform: str) -> None:
    # made-up steps with speed, torque, current and voltage: power is volts times amps and
    # the shaft power (torque times speed) is under it
    rows = [{"throttle_pct": t, "thrust_g": g, "power_w": v * a, "rpm": n, "torque_nm": q,
             "current_a": a, "volts": v}
            for t, g, n, q, a, v in ((40, 2000, 3400, 0.70, 5.5, 48.0),
                                     (60, 4000, 4700, 1.35, 16.0, 47.8),
                                     (100, 9000, 6900, 3.00, 55.0, 47.0))]  # fmt: skip
    got = bench.save(store, platform, table(0.002, rows=rows))
    first = got.rows[0]
    assert (first.rpm, first.torque_nm, first.current_a, first.volts) == (3400, 0.70, 5.5, 48.0)
    assert first.shaft_w == pytest.approx(0.70 * 3400 * 2 * math.pi / 60)
    assert got.view()["rows"][0] == rows[0]
    again = bench.benches(store, platform)[0]  # read back from the store
    assert again.rows == got.rows
    # a table without them reads as before, and its rows hold only the three columns
    plain = bench.save(store, platform, table(0.002, label="plain"))
    assert set(plain.view()["rows"][0]) == {"throttle_pct", "thrust_g", "power_w"}
    assert plain.rows[0].shaft_w is None


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"rpm": -1}, "above zero"),
        ({"current_a": 0}, "above zero"),
        # 30 N·m at 3400 rpm is 10.7 kW, past the 264 W drawn: a torque in another unit
        ({"torque_nm": 30.0}, "torque times speed"),
    ],
)
def test_the_makers_other_columns_are_checked(
    store: Store, platform: str, change: dict[str, float], says: str
) -> None:
    rows = table(0.002)["rows"]
    rows[0] = rows[0] | {"rpm": 3400.0, "torque_nm": 0.7, "current_a": 5.5, "volts": 48.0} | change
    with pytest.raises(bench.BenchError, match=says):
        bench.save(store, platform, table(0.002, rows=rows))


def test_a_tables_propeller_family_and_blades(store: Store, platform: str) -> None:
    families = ("tmotor_p", "carbon")
    got = bench.save(store, platform, table(0.002, prop_family="tmotor_p", prop_blades=2),
                     families=families)  # fmt: skip
    assert (got.prop_family, got.prop_blades) == ("tmotor_p", 2)
    assert got.view()["prop_family"] == "tmotor_p" and got.view()["prop_blades"] == 2
    assert bench.benches(store, platform)[0] == got  # read back from the store
    plain = bench.save(store, platform, table(0.002, label="no family", prop_family=""))
    assert (plain.prop_family, plain.prop_blades) == (None, None)
    refused: list[tuple[dict[str, Any], str]] = [
        ({"prop_family": "tmotor_x"}, "propeller families"),
        ({"prop_blades": 1}, "2 to 8"), ({"prop_blades": 2.5}, "whole"),
        ({"prop_blades": True}, "whole"),
    ]  # fmt: skip
    for change, says in refused:
        with pytest.raises(bench.BenchError, match=says):
            bench.save(store, platform, table(0.002, **change), families=families)
