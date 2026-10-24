"""Board-ID hashes made again with the fleet's key when a laptop joins a fleet: from
each log still on disk, with the flight key, cache, airframe and this computer's
station following; a flight whose log is gone keeps its hash and is still matched."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_imports import KEY, LAPTOP, airframe, run, variant
from test_stations import FACTS, MP

from flight_insights.fingerprint import hash_board_id
from flight_insights.imports import propose, scan
from flight_insights.imports.rekey import needs_rekey, rekey
from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms import load_schema
from flight_insights.stations import (
    create_station,
    hash_host,
    record_computer,
    station_view,
)
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FLEET = bytes(range(100, 132))
HOST = "GCS-LAPTOP"


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture
def keys() -> StoreKeys:
    return StoreKeys.generate()


@pytest.fixture
def store(tmp_path: Path, keys: StoreKeys) -> Iterator[Store]:
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as s:
        yield s


@pytest.fixture
def cache(tmp_path: Path, keys: StoreKeys) -> ColumnCache:
    return ColumnCache(tmp_path / "cache", keys)


def flight_row(store: Store, fid: str) -> dict[str, Any]:
    row = store.get("flight", fid)
    assert row is not None
    return row


def test_hashes_are_made_again_from_the_logs(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    assert not needs_rekey(store, KEY)  # the first key a store sees is its own
    aid = airframe(store, pack)
    log = variant(tmp_path, "a", system_id="board-A")
    (_, _, (result,)) = run(store, pack, cache, [log], {"f1": {"airframe_id": aid}})
    (fid,) = result.flights
    before = flight_row(store, fid)
    assert before["board_id_hash"] == hash_board_id(KEY, "board-A")
    assert cache.names(before["flight_key"])
    schema = load_schema(pack, "station")
    sid = create_station(store, "GCS")
    record_computer(store, schema, sid, station_view(store, sid)["version"], FACTS, MP,
                    hash_host(KEY, HOST))  # fmt: skip

    assert needs_rekey(store, FLEET)
    done = rekey(store, cache, pack, FLEET, KEY, HOST)
    assert (done.flights, done.kept, done.stations) == (1, 0, 1)
    after = flight_row(store, fid)
    assert after["board_id_hash"] == hash_board_id(FLEET, "board-A")
    assert after["flight_key"] != before["flight_key"]
    assert cache.names(after["flight_key"]) and not cache.names(before["flight_key"])
    a = store.get("airframe", aid)
    assert a is not None and a["board_id_hash"] == after["board_id_hash"]
    assert station_view(store, sid, hash_host(FLEET, HOST))["this_computer"]
    assert not needs_rekey(store, FLEET)
    # the change is captured to be sent, like any edit
    pending = store.conn.execute(
        "SELECT field FROM sync_pending WHERE tbl = 'flight' AND rid = ?", (fid,)
    ).fetchall()
    assert {"flight_key", "board_id_hash"} <= {r[0] for r in pending}


def test_a_flight_whose_log_is_gone_keeps_its_hash_and_still_matches(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    log = variant(tmp_path, "a", system_id="board-A")
    run(store, pack, cache, [log], {"f1": {"airframe_id": aid}})
    log.unlink()
    done = rekey(store, cache, pack, FLEET, KEY, None)
    assert (done.flights, done.kept) == (0, 1)
    # a later log from the same board, scanned with the fleet's key and the old one
    later = variant(tmp_path, "b", system_id="board-A", ATC_RAT_RLL_P=0.2)
    batch = scan(store, pack, FLEET, [later], legacy_key=KEY)
    (proposal,) = propose(store, pack, batch)
    assert (proposal.airframe_id, proposal.how) == (aid, "board")
    assert not any(c.part == "board" for c in proposal.changes)
    # without the old key, the board is not recognised
    (blind,) = propose(store, pack, scan(store, pack, FLEET, [later]))
    assert blind.how != "board"


def test_joining_a_fleet_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import Output, call

    from flight_insights.rpc import build_server

    own = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    log = variant(tmp_path, "a", system_id="board-A")
    data = tmp_path / "data"

    def engine(keys: dict[str, str]) -> tuple[Any, Output, dict[str, Any]]:
        out = Output()
        server = build_server(out, pack)
        got = call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(data),
                   keys=keys, host=HOST)["result"]  # fmt: skip
        return server, out, got

    server, out, got = engine(own)
    assert got["rekey_needed"] is False
    platform = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=platform["id"], name="Q-1")["result"]
    scanned = call(server, out, 4, "import.scan", paths=[str(log)])["result"]
    call(server, out, 5, "import.commit", batch=scanned["batch"],
         decisions=[{"log": "f1", "airframe_id": af["id"]}])  # fmt: skip
    call(server, out, 6, "engine.shutdown")

    fleet = own | {"board_id": FLEET.hex(), "legacy_board_id": own["board_id"]}
    server, out, got = engine(fleet)
    assert got["rekey_needed"] is True
    done = call(server, out, 2, "board.rekey", host=HOST)["result"]
    assert (done["flights"], done["kept"]) == (1, 0)
    call(server, out, 3, "engine.shutdown")
    server, out, got = engine(fleet)
    assert got["rekey_needed"] is False
    call(server, out, 2, "engine.shutdown")
