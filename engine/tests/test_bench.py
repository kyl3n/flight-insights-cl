"""Makers' bench tables and the comparison of two builds (performance/bench.py). Every
table here is made up: power grows as thrust to the 1.5, so log-log reading between the
steps is exact and the ratio of two tables is known at any thrust."""

from __future__ import annotations

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
    c = bench.compare(now, then, 4062.5)
    assert c["share"] == pytest.approx(0.9, rel=1e-9) and c["notes"] == []
    short = bench.save(store, platform, table(0.0016, (1500.0, 2500.0, 3500.0), top=62,
                                              prop_diameter_in=21, volts=44))  # fmt: skip
    c = bench.compare(now, short, 4062.5)
    assert c["share"] is None and c["candidate_w"] is None
    assert c["notes"] == ["props_differ", "volts_differ", "candidate_out_of_range",
                          "candidate_short"]  # fmt: skip
    assert bench.compare(now, then, 1000.0)["notes"] == ["current_out_of_range",
                                                         "candidate_out_of_range"]  # fmt: skip


def test_only_the_propulsion_power_scales() -> None:
    m = Model(avionics=60.0, induced=1.0, flight=2.0, profile=3.0, growth=4.0,
              drag_area=0.5, disc_area=1.2, rms=0.03)  # fmt: skip
    got = bench.propulsion(m, 0.9)
    assert got == replace(m, induced=0.9, flight=1.8, profile=2.7)


def test_bench_tables_sync() -> None:
    assert "bench_table" in synced.RECORDS
