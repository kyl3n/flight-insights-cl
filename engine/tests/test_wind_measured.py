"""The measured wind per flight, beside the model (weather/measured.py): a flight that
measured its own gives it; a hover borrows its airframe's drag from a flight in the same
payload band and on the same build, and reads the simulator's set wind with it; nothing
is borrowed across airframes or builds; and the difference from the modelled wind."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_imports import airframe

from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.run import run_analysis
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys
from flight_insights.weather import measured

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
PACK = load_pack()


def flown(store: Store, cache: ColumnCache, pack: Pack, name: str, af: str) -> str:
    batch = scan(store, pack, bytes(32), [FIXTURES / f"{name}.bin"])
    [res] = commit(store, pack, cache, batch, propose(store, pack, batch),
                   [Decision(batch[0].id, airframe_id=af)])  # fmt: skip
    flight = res.flights[0]
    run_analysis(store, cache, pack, flight)
    return flight


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> tuple[Store, ColumnCache, dict[str, str]]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("measured")
    store = open_store(folder / "m.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    quad = airframe(store, PACK, "SITL quad")
    other = airframe(store, PACK, "another quad")
    ids = {
        "calm": flown(store, cache, PACK, "copter-quad-wind-calm", quad),
        "hover": flown(store, cache, PACK, "copter-quad-wind-hover", quad),
        "alone": flown(store, cache, PACK, "copter-quad-hover", other),
    }
    return store, cache, ids


def test_a_flight_that_measured_its_own_gives_it(
    fleet: tuple[Store, ColumnCache, dict[str, str]],
) -> None:
    store, cache, ids = fleet
    got = measured.measured(store, cache, PACK, ids["calm"])
    assert got is not None and got["source"] == "flight" and got["drag_from"] == []
    assert got["speed_ms"] < 0.3 and got["windows"] >= 100


def test_a_hover_reads_the_wind_with_its_airframes_drag(
    fleet: tuple[Store, ColumnCache, dict[str, str]],
) -> None:
    store, cache, ids = fleet
    got = measured.measured(store, cache, PACK, ids["hover"])
    assert got is not None and got["source"] == "airframe"
    assert got["drag_from"] == [ids["calm"]]
    assert got["speed_ms"] == pytest.approx(5.0, rel=0.05)  # SITL's 5 m/s, gusting
    assert abs(got["from_deg"] - 270) <= 5 and got["peak_ms"] >= got["speed_ms"]
    assert got["windows"] >= 10 and got["height_m"] == pytest.approx(20, abs=1)


def test_nothing_is_borrowed_from_another_airframe(
    fleet: tuple[Store, ColumnCache, dict[str, str]],
) -> None:
    store, cache, ids = fleet
    assert measured.borrowed(store, Registry.load(PACK), ids["alone"]) is None
    assert measured.measured(store, cache, PACK, ids["alone"]) is None


def test_nor_from_another_build(tmp_path: Path) -> None:
    """New props or arms change the drag: a flight on a later build borrows nothing from
    an earlier one."""
    keys = StoreKeys.generate()
    with open_store(tmp_path / "b.db", keys, "e" * 32) as store:
        cache = ColumnCache(tmp_path / "cache", keys)
        quad = airframe(store, PACK, "SITL quad")
        flown(store, cache, PACK, "copter-quad-wind-calm", quad)
        hover = flown(store, cache, PACK, "copter-quad-wind-hover", quad)
        assert measured.borrowed(store, Registry.load(PACK), hover) is not None
        build = store.insert(
            "build_version",
            {"airframe_id": quad, "number": 2, "reason_kind": "upgrade", "reason": "new props",
             "spec_json": "{}", "effective_date": "2026-10-27", "first_flight_id": hover},
        )  # fmt: skip
        row = store.get("flight", hover)
        assert row is not None
        store.update("flight", hover, int(row["version"]), {"build_version_id": build})
        assert measured.borrowed(store, Registry.load(PACK), hover) is None


def test_the_model_against_the_measured() -> None:
    own = {"speed_ms": 5.0, "from_deg": 350}
    assert measured.difference(own, {"wind_ms": 6.2, "wind_from_deg": 10}) == {
        "speed_ms": 1.2, "from_deg": 20,
    }  # fmt: skip
    assert measured.difference(own, {"wind_ms": 4.0, "wind_from_deg": 330}) == {
        "speed_ms": -1.0, "from_deg": -20,
    }  # fmt: skip
    calm = {"speed_ms": 0.2, "from_deg": 90}  # no direction to speak of
    assert measured.difference(calm, {"wind_ms": 1.0, "wind_from_deg": 270}) == {"speed_ms": 0.8}
    assert measured.difference(own, {"quality": "none", "coverage": 0.0}) is None
    assert measured.difference(None, {"wind_ms": 3.0}) is None


def test_the_record_method_gives_the_measured_wind(tmp_path: Path) -> None:
    """flight.weather: no downloads here, so the model has nothing, but the aircraft's
    own reading is there, with its airframe's drag."""
    from typing import Any

    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    n = iter(range(1, 100))

    def rpc(method: str, **params: object) -> Any:
        reply = call(server, out, next(n), method, **params)
        assert "result" in reply, reply
        return reply["result"]

    rpc("engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    pl = rpc("platform.create", name="SITL", vehicle="copter")
    af = rpc("airframe.create", platform_id=pl["id"], name="SITL quad")
    ids: list[str] = []
    for name in ("copter-quad-wind-calm", "copter-quad-wind-hover"):
        batch = rpc("import.scan", paths=[str(FIXTURES / f"{name}.bin")])["batch"]
        [done] = rpc(
            "import.commit", batch=batch, decisions=[{"log": "f1", "airframe_id": af["id"]}]
        )
        ids.append(str(done["flights"][0]))
        rpc("analysis.run", flight_id=ids[-1])
    got = rpc("flight.weather", flight_id=ids[1])
    assert got["record"] == {"quality": "none", "coverage": 0.0, "kp_max": None}
    assert got["measured"]["source"] == "airframe" and got["measured"]["drag_from"] == [ids[0]]
    assert got["measured"]["speed_ms"] == pytest.approx(5.0, rel=0.05)
    assert got["difference"] is None  # no model to compare with
