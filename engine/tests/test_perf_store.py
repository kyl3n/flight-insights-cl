"""The airframe's power model, made from its flights and kept until they change. ArduPilot's
simulator flies Callisto (32.5 kg, 30-inch propellers on four coaxial arms) through its
speed card light and 8 kg heavier: both flights give points, the model fits over them,
and the RPC gives its summary and the points as an Arrow file with no time or heading.
Weighing a flight again makes the model again; asking twice with nothing changed reads
the one kept."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from flight_insights.analyzers.run import run_analysis
from flight_insights.imports.commit import Decision, commit
from flight_insights.imports.propose import propose
from flight_insights.imports.scan import scan
from flight_insights.knowledge import load_pack
from flight_insights.performance import airframe as perf
from flight_insights.platforms.records import (
    create_airframe,
    create_platform,
    platform_view,
    save_answers,
)
from flight_insights.platforms.schema import load_schema
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store, open_store
from flight_insights.store.keys import StoreKeys

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
PACK = load_pack()
CALLISTO = {"wheelbase_mm": 1325, "prop_diameter_in": 30}


def airframe(store: Store, name: str, answers: dict[str, Any]) -> str:
    schema = load_schema(PACK)
    platform = create_platform(store, schema, name, "copter")
    version = platform_view(store, schema, platform)["reference_build"]["version"]
    save_answers(store, schema, platform, version, answers)
    return create_airframe(store, platform, name)


def flown(store: Store, cache: ColumnCache, name: str, af: str, kg: float | None) -> str:
    batch = scan(store, PACK, bytes(32), [FIXTURES / f"{name}.bin"])
    [res] = commit(store, PACK, cache, batch, propose(store, PACK, batch),
                   [Decision(batch[0].id, airframe_id=af, takeoff_kg=kg)])  # fmt: skip
    flight = res.flights[0]
    run_analysis(store, cache, PACK, flight)
    return flight


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("perf")
    store = open_store(folder / "p.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    af = airframe(store, "Callisto", CALLISTO)
    light = flown(store, cache, "copter-heavylift-sweep", af, 32.5)
    loaded = flown(store, cache, "copter-heavylift-sweep-loaded", af, 40.5)
    return {"store": store, "cache": cache, "airframe": af, "light": light, "loaded": loaded,
            "folder": folder, "keys": keys}  # fmt: skip


def test_both_flights_give_points_and_the_model_fits_over_them(fleet: dict[str, Any]) -> None:
    m = perf.load(fleet["store"], fleet["cache"], PACK, fleet["airframe"])
    assert m.why is None and m.fitted is not None and m.coaxial
    # four coaxial arms of 30-inch propellers: four discs
    assert m.disc_area == pytest.approx(4 * math.pi * (30 * 0.0254 / 2) ** 2)
    by = {f.flight_id: f for f in m.flights}
    assert by[fleet["light"]].mass_kg == 32.5 and by[fleet["loaded"]].mass_kg == 40.5
    assert all(len(f.points) >= 3 for f in m.flights)
    assert m.fitted.flights == 2 and m.fitted.rows == sum(len(f.points) for f in m.flights)
    assert set(m.fitted.lofo) == {fleet["light"], fleet["loaded"]}
    # the simulator's motors draw power in proportion to thrust: the model fits it within
    # a few per cent, whatever it makes of the speed
    assert m.fitted.model.rms < 0.05


def test_kept_until_its_flights_change(fleet: dict[str, Any]) -> None:
    store, cache, af = fleet["store"], fleet["cache"], fleet["airframe"]
    first = perf.load(store, cache, PACK, af)
    kept = store.conn.execute("SELECT basis FROM performance_fit WHERE airframe_id = ?",
                              (af,)).fetchone()  # fmt: skip
    assert kept is not None and kept[0] == perf.basis(store, PACK, af)
    again = perf.load(store, cache, PACK, af)
    assert perf.to_json(again) == perf.to_json(first)  # read back the same
    row = store.get("flight", fleet["loaded"])
    assert row is not None
    store.update("flight", row["id"], row["version"], {"takeoff_kg": 41.0})
    assert perf.basis(store, PACK, af) != kept[0]
    changed = perf.load(store, cache, PACK, af)
    assert {f.flight_id: f.mass_kg for f in changed.flights}[fleet["loaded"]] == 41.0


def test_without_the_propellers_size_there_is_no_model(fleet: dict[str, Any]) -> None:
    store, cache = fleet["store"], fleet["cache"]
    af = airframe(store, "Callisto, no props", {"wheelbase_mm": 1325})
    flown(store, cache, "copter-heavylift-discharge", af, 32.5)  # a log not yet imported
    m = perf.load(store, cache, PACK, af)
    assert m.fitted is None and m.why is not None and "propeller diameter" in m.why


def test_the_rpc_gives_the_summary_and_the_points(tmp_path: Path) -> None:
    from test_rpc import LAPTOP, Output, call
    from test_rpc import keys as store_keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, PACK)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path),
         keys=store_keys())  # fmt: skip
    pl = call(server, out, 2, "platform.create", name="Callisto", vehicle="copter")["result"]
    version = pl["reference_build"]["version"]
    call(server, out, 3, "platform.save", id=pl["id"], build_version=version, values=CALLISTO)
    af = call(server, out, 4, "airframe.create", platform_id=pl["id"], name="C-1")["result"]["id"]
    for i, (name, kg) in enumerate((("copter-heavylift-sweep", 32.5),
                                    ("copter-heavylift-sweep-loaded", 40.5))):  # fmt: skip
        scanned = call(server, out, 10 + i, "import.scan",
                       paths=[str(FIXTURES / f"{name}.bin")])["result"]  # fmt: skip
        decision = {"log": "f1", "airframe_id": af, "takeoff_kg": kg}
        [done] = call(server, out, 20 + i, "import.commit", batch=scanned["batch"],
                      decisions=[decision])["result"]  # fmt: skip
        call(server, out, 30 + i, "analysis.run", flight_id=done["flights"][0])
    s = call(server, out, 40, "airframe.performance_model", airframe_id=af)["result"]
    assert s["why"] is None and s["fit"]["level"] in ("A", "B") and s["fit"]["flights"] == 2
    assert set(s["fit"]["coefficients"]) == {"avionics", "induced", "flight", "profile",
                                             "growth", "drag_area"}  # fmt: skip
    assert sorted(f["mass_kg"] for f in s["flights"]) == [32.5, 40.5]
    p = call(server, out, 41, "airframe.performance_points", airframe_id=af)["result"]
    with pa.memory_map(p["path"]) as source:
        table = pa.ipc.open_file(source).read_all()
    assert table.num_rows == p["rows"] == s["fit"]["rows"]
    assert set(table.column_names) == {"flight", "phase", "airspeed_ms", "climb_ms", "power_w",
                                       "density", "mass_kg", "seconds"}  # fmt: skip
    bad = call(server, out, 42, "airframe.performance_model", airframe_id="")
    assert bad["error"]["code"] == -32602
    call(server, out, 43, "files.release", path=p["path"])
    call(server, out, 44, "engine.shutdown")
