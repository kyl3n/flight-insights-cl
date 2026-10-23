"""Import: device IDs, the airframe proposal, storing a batch, the hold queue and
the RPC methods. Logs are SITL fixtures, copied and patched in place to give them
a board ID or a different sensor."""

from __future__ import annotations

import json
import secrets
import struct
import sys
from pathlib import Path
from typing import Any, LiteralString

import numpy as np
import pytest
from df_builder import LogBuilder

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.formats import text
from flight_insights.fingerprint.board import system_id_row, system_id_rows
from flight_insights.fingerprint.devices import (
    Hardware,
    can_nodes,
    compare,
    decode_device_id,
    from_json,
    to_json,
)
from flight_insights.imports import (
    ConfirmedChange,
    Decision,
    Result,
    commit,
    held,
    propose,
    resolve,
    scan,
)
from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms import create_airframe, create_platform, load_schema
from flight_insights.store import Store, StoreKeys, _sqlcipher, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
QUAD = FIXTURES / "copter-quad-hover"
LAPTOP = "f" * 32
KEY = bytes(range(32))
DAY_US = 86_400_000_000


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture
def keys() -> StoreKeys:
    return StoreKeys.generate()


@pytest.fixture
def store(tmp_path: Path, keys: StoreKeys) -> Any:
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as s:
        yield s


@pytest.fixture
def cache(tmp_path: Path, keys: StoreKeys) -> ColumnCache:
    return ColumnCache(tmp_path / "cache", keys)


def field_offset(dtype: np.dtype[Any], name: LiteralString) -> int:
    fields = dtype.fields
    assert fields is not None
    return int(fields[name][1])


def variant(tmp_path: Path, name: str, system_id: str | None = None, **params: float) -> Path:
    """A copy of the quad fixture with its board's system ID and some parameters
    rewritten in place, as another board or a changed sensor would log them."""
    data = bytearray(QUAD.with_suffix(".bin").read_bytes())
    with read_dataflash(QUAD.with_suffix(".bin")) as df:
        if system_id is not None:
            row = system_id_row(df)
            assert row is not None
            fid, position = row
            fmt = df.index.formats[fid]
            at = int(df.offsets(fid)[position]) + 3 + field_offset(fmt.dtype, "Message")
            data[at : at + 64] = system_id.encode().ljust(64, b"\0")
        for fid in df.formats_named("PARM"):
            fmt = df.index.formats[fid]
            rows = df.rows(fid)
            value_at = field_offset(fmt.dtype, "Value")
            for k in range(len(rows)):
                pname = text(rows["Name"][k].tobytes())
                if pname in params:
                    at = int(df.offsets(fid)[k]) + 3 + value_at
                    data[at : at + 4] = struct.pack("<f", params[pname])
    out = tmp_path / f"{name}.bin"
    out.write_bytes(data)
    return out


def bench(tmp_path: Path) -> Path:
    """The quad fixture cut off before it armed, logging while disarmed: a log with
    parameters but no flight."""
    data = variant(tmp_path, "full", LOG_DISARMED=1).read_bytes()
    with read_dataflash(QUAD.with_suffix(".bin")) as df:
        cut = min(
            int(df.offsets(f)[0])
            for name in ("ARM", "EV")
            for f in df.formats_named(name)
            if df.count_format(f)
        )
    out = tmp_path / "bench.bin"
    out.write_bytes(data[:cut])
    return out


def count(store: Store, sql: str, *args: Any) -> int:
    return store.conn.execute(sql, args).fetchone()[0]


def airframe(store: Store, pack: Pack, name: str = "Q-1") -> str:
    platform = create_platform(store, load_schema(pack), "Quad", "copter")
    return create_airframe(store, platform, name)


def run(store: Store, pack: Pack, cache: ColumnCache, paths: list[Path], decide: dict[str, Any]):
    batch = scan(store, pack, KEY, paths)
    proposals = propose(store, pack, batch)
    decisions = [Decision(log, **d) for log, d in decide.items()]
    return batch, proposals, commit(store, pack, cache, batch, proposals, decisions)


# --- device IDs -----------------------------------------------------------------------


def test_device_ids_unpack_as_the_firmware_packs_them() -> None:
    d = decode_device_id(2753028)  # the SITL accelerometer's INS_ACC_ID
    assert (d.bus_type, d.bus, d.address, d.devtype) == ("SITL", 0, 2, 0x2A)
    spi = decode_device_id((0x09 << 16) | (0x01 << 8) | (1 << 3) | 2)
    assert (spi.bus_type, spi.bus, spi.address, spi.devtype) == ("SPI", 1, 1, 9)
    assert "SPI bus 1" in spi.describe()


def test_hardware_round_trips_and_changes_are_listed_with_evidence() -> None:
    a = Hardware("4.7.0", {"INS_ACC_ID": decode_device_id(2753028)}, ())
    b = Hardware("4.7.2", {"INS_ACC_ID": decode_device_id(2753036)}, ())
    assert from_json(json.loads(json.dumps(to_json(a)))) == a
    changes = {c.part: c for c in compare(a, b, "bid1-a", "bid1-b")}
    assert set(changes) == {"board", "firmware", "INS_ACC_ID"}
    assert changes["INS_ACC_ID"].evidence == "parameter INS_ACC_ID"
    assert changes["firmware"].before == "4.7.0" and changes["firmware"].after == "4.7.2"
    assert compare(a, a, "bid1-a", "bid1-a") == []


def test_can_nodes_come_from_cand_without_their_unique_ids(tmp_path: Path) -> None:
    b = LogBuilder()
    b.fmt(0x90, "CAND", "QBQQZBB", "TimeUS,NodeId,UID1,UID2,Name,Major,Minor")
    b.write("CAND", 1, 10, 0xDEADBEEF, 0xFEEDF00D, b"org.ardupilot.here4", 1, 9)
    b.write("CAND", 2, 10, 0xDEADBEEF, 0xFEEDF00D, b"org.ardupilot.here4", 1, 9)
    b.write("CAND", 3, 125, 1, 2, b"com.hex.esc", 2, 0)
    path = tmp_path / "can.bin"
    path.write_bytes(b.bytes())
    with read_dataflash(path) as df:
        nodes = can_nodes(df)
    assert [n.describe() for n in nodes] == [
        "node 10 org.ardupilot.here4 1.9",
        "node 125 com.hex.esc 2.0",
    ]
    assert "DEADBEEF" not in json.dumps(to_json(Hardware("4.7.0", {}, nodes))).upper()


def test_a_fixture_has_its_sensors(pack: Pack, store: Store) -> None:
    (item,) = scan(store, pack, KEY, [QUAD.with_suffix(".bin")])
    assert item.hardware is not None
    assert {"INS_ACC_ID", "INS_GYR_ID", "COMPASS_DEV_ID"} <= set(item.hardware.sensors)
    assert item.sysid == 1 and len(item.flights) == 1


# --- proposals ------------------------------------------------------------------------


def test_a_first_log_matches_nothing_and_is_held(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    log = variant(tmp_path, "a", system_id="board-A")
    _, (p,), (r,) = run(store, pack, cache, [log], {})
    assert p.status == "no match" and r.status == "held"
    (h,) = held(store)
    assert h["reason"] == "no match" and h["log"] == "a.bin"


def test_a_type_no_file_can_name_is_left_out_and_reported(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted FMT can name a type anything: that type is not cached, and the
    import says so instead of failing."""

    def cacheable(name: str) -> bool:
        return name != "GPS"

    monkeypatch.setattr(ColumnCache, "cacheable", staticmethod(cacheable))
    aid = airframe(store, pack)
    _, _, (r,) = run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    assert r.status == "imported" and r.flights
    assert "'GPS'" in (r.message or "")
    flight = store.get("flight", r.flights[0])
    assert flight is not None
    names = cache.names(flight["flight_key"])
    assert "GPS" not in names and "ATT" in names


def test_the_board_id_finds_its_airframe(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    # the next boot of the same board, with a new accelerometer
    later = variant(tmp_path, "b", system_id="board-A", STAT_BOOTCNT=1234, INS_ACC_ID=2753036)
    (p,) = propose(store, pack, scan(store, pack, KEY, [later]))
    assert (p.status, p.how, p.confidence, p.airframe_id) == ("ready", "board", "high", aid)
    (change,) = p.changes
    assert change.part == "INS_ACC_ID" and "SITL bus 1" in (change.after or "")


def test_the_same_boot_again_is_a_duplicate(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    copy = variant(tmp_path, "b", system_id="board-A", INS_ACC_ID=2753036)
    (p,) = propose(store, pack, scan(store, pack, KEY, [copy]))
    assert p.status == "duplicate"  # same board, same boot: the same flight


def test_a_new_board_with_the_same_tuning_asks_a_person(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    batch = scan(store, pack, KEY, [variant(tmp_path, "b", system_id="board-B")])
    (p,) = propose(store, pack, batch)
    assert p.status == "decision" and p.airframe_id == aid and p.how == "parameters"
    assert p.score is not None and p.score >= 0.9
    assert "flight controller replaced" in (p.note or "")
    assert any(c.part == "board" for c in p.changes)


def test_without_board_ids_tuning_is_a_medium_match(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(store, pack, cache, [QUAD.with_suffix(".bin")], {"f1": {"airframe_id": aid}})
    later = variant(tmp_path, "b", STAT_BOOTCNT=1234)  # no board ID to go on
    (p,) = propose(store, pack, scan(store, pack, KEY, [later]))
    assert (p.status, p.how, p.confidence, p.airframe_id) == ("ready", "parameters", "medium", aid)
    assert p.changes == []


def test_different_tuning_matches_nothing(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(store, pack, cache, [QUAD.with_suffix(".bin")], {"f1": {"airframe_id": aid}})
    plane = FIXTURES / "plane-circuit.bin"
    (p,) = propose(store, pack, scan(store, pack, KEY, [plane]))
    assert p.status == "no match" and p.candidates and p.candidates[0].score < 0.9


# --- storing a batch ------------------------------------------------------------------


def test_a_batch_is_stored_with_its_tlog_and_parameter_file(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    paths = [QUAD.with_suffix(e) for e in (".tlog", ".bin", ".param")]
    batch, proposals, results = run(
        store,
        pack,
        cache,
        paths,
        {
            "f2": {"airframe_id": aid, "packs": ["P-12", "P-13"], "payload": "none"},
            "f3": {"airframe_id": aid},
        },
    )
    status = {r.log: r.status for r in results}
    assert status == {"f1": "linked", "f2": "imported", "f3": "imported"}
    assert proposals[0].status == "ready"  # the tlog pairs with the log in the same batch
    (flight,) = store.conn.execute("SELECT id, airframe_id, payload FROM flight").fetchall()
    assert flight[1] == aid and flight[2] == "none"
    roles = sorted(r[0] for r in store.conn.execute("SELECT role FROM flight_source"))
    assert roles == ["dataflash", "tlog"]
    assert store.conn.execute("SELECT count(*) FROM flight_battery").fetchone()[0] == 2
    assert (
        store.conn.execute(
            "SELECT count(*) FROM param_snapshot WHERE airframe_id = ?", (aid,)
        ).fetchone()[0]
        == 1
    )
    # importing again changes nothing
    _, again, results = run(store, pack, cache, paths, {"f2": {"airframe_id": aid}})
    assert {p.status for p in again} == {"duplicate"}
    assert store.conn.execute("SELECT count(*) FROM flight").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM source_file").fetchone()[0] == 3
    assert (
        batch[1].flights[0].key == store.conn.execute("SELECT flight_key FROM flight").fetchone()[0]
    )


def test_a_tlog_imported_first_is_paired_when_its_log_arrives(
    pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    _, (p,), (r,) = run(store, pack, cache, [QUAD.with_suffix(".tlog")], {})
    assert p.status == "unpaired" and r.status == "imported"
    run(store, pack, cache, [QUAD.with_suffix(".bin")], {"f1": {"airframe_id": aid}})
    roles = sorted(r[0] for r in store.conn.execute("SELECT role FROM flight_source"))
    assert roles == ["dataflash", "tlog"]


def test_the_cache_holds_every_type_but_never_the_system_id(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    log = variant(tmp_path, "a", system_id="SECRET-BOARD-ID-1234")
    batch, _, _ = run(store, pack, cache, [log], {})
    key = batch[0].flights[0].key
    with read_dataflash(log) as df:
        types = {name for name in df.message_types if name != "FMT"}
        msgs = len(df.table("MSG"))
    assert types <= set(cache.names(key)) | {"FMT"}
    rows = cache.read(key, "MSG").column("Message").to_pylist()
    messages = [text(m) for m in rows if isinstance(m, bytes)]
    assert len(messages) == len(rows)
    assert len(messages) == msgs - 1
    assert not any("SECRET" in m for m in messages)
    for f in (tmp_path / "cache").rglob("*.parquet"):
        assert b"SECRET-BOARD" not in f.read_bytes()


def test_confirmed_changes_make_a_build_version(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    second = variant(tmp_path, "b", system_id="board-B")
    run(
        store,
        pack,
        cache,
        [second],
        {
            "f1": {
                "airframe_id": aid,
                "changes": [
                    ConfirmedChange("board", "failure", "crash damage"),
                    ConfirmedChange("propellers", "upgrade", "carbon 28 in"),
                ],
            }
        },
    )
    builds = store.conn.execute(
        "SELECT number, reason_kind, reason, first_flight_id FROM build_version "
        "WHERE airframe_id = ? ORDER BY number",
        (aid,),
    ).fetchall()
    assert [b[0] for b in builds] == [1, 2]
    assert builds[1][1] == "detected" and "crash damage" in builds[1][2]
    assert builds[1][3] is not None
    row = store.get("airframe", aid)
    assert (
        row is not None
        and row["board_id_hash"] == scan(store, pack, KEY, [second])[0].board_id_hash
    )


def test_an_unconfirmed_new_board_is_still_recorded(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "b", system_id="board-B")],
        {"f1": {"airframe_id": aid}},
    )
    reasons = [
        r[0]
        for r in store.conn.execute(
            "SELECT reason FROM build_version WHERE airframe_id = ? ORDER BY number", (aid,)
        )
    ]
    assert "flight controller replaced" in (reasons[-1] or "")


def test_held_flights_are_resolved_later(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    run(store, pack, cache, [variant(tmp_path, "b", system_id="board-B")], {})  # undecided
    (h,) = held(store)
    assert h["reason"] == "decision" and h["candidates"][0]["airframe_id"] == aid
    platform = store.get("airframe", aid)
    assert platform is not None
    new = resolve(store, pack, [h["id"]], new_airframe=(platform["platform_id"], "Q-2"))
    assert new != aid and held(store) == []
    with pytest.raises(ValueError, match="not waiting"):
        resolve(store, pack, [h["id"]], airframe_id=aid)


def test_skipped_and_unreadable_files_are_reported(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("hello", encoding="utf-8")
    broken = tmp_path / "broken.bin"
    broken.write_bytes(b"\x00" * 100)
    _, proposals, results = run(
        store, pack, cache, [junk, broken, QUAD.with_suffix(".bin")], {"f3": {"action": "skip"}}
    )
    assert proposals[0].status == "error"
    assert [r.status for r in results] == ["error", "error", "skipped"]
    assert "no VER message" in (results[1].message or "")
    assert store.conn.execute("SELECT count(*) FROM flight").fetchone()[0] == 0


def test_a_flight_stored_since_the_scan_is_a_duplicate(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    log = variant(tmp_path, "a", system_id="board-A")
    copy = tmp_path / "copy.bin"
    copy.write_bytes(log.read_bytes())
    decide: dict[str, Any] = {"airframe_id": aid}
    _, _, results = run(store, pack, cache, [log, copy], {"f1": decide, "f2": decide})
    assert [r.status for r in results] == ["imported", "duplicate"]
    # two batches of the same log, both scanned before either is stored
    other = variant(tmp_path, "b", system_id="board-A", STAT_BOOTCNT=1234)
    first, second = (scan(store, pack, KEY, [other]) for _ in range(2))
    for batch in (first, second):
        proposals = propose(store, pack, batch)
        (r,) = commit(store, pack, cache, batch, proposals, [Decision("f1", airframe_id=aid)])
    assert r.status == "duplicate"
    assert count(store, "SELECT count(*) FROM flight") == 2
    assert count(store, "SELECT count(*) FROM flight_source") == 2


def test_a_store_error_fails_only_its_own_file(
    pack: Pack, store: Store, cache: ColumnCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any) -> Result:
        raise _sqlcipher.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(sys.modules["flight_insights.imports.commit"], "_tlog", broken)
    aid = airframe(store, pack)
    paths = [QUAD.with_suffix(e) for e in (".bin", ".tlog")]
    _, _, results = run(store, pack, cache, paths, {"f1": {"airframe_id": aid}})
    assert [r.status for r in results] == ["imported", "error"]
    assert "IntegrityError" in (results[1].message or "")


def test_a_bench_log_is_imported_as_the_chosen_airframe_s_snapshot(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    _, _, (r,) = run(store, pack, cache, [bench(tmp_path)], {"f1": {"airframe_id": aid}})
    assert (r.status, r.flights) == ("imported", [])
    assert count(store, "SELECT count(*) FROM param_snapshot WHERE airframe_id = ?", aid) == 1


def test_a_parameter_file_is_kept_once_per_airframe(
    pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    param = QUAD.with_suffix(".param")
    for status in ("imported", "duplicate"):
        _, _, (r,) = run(store, pack, cache, [param], {"f1": {"airframe_id": aid}})
        assert r.status == status
    assert count(store, "SELECT count(*) FROM param_snapshot WHERE airframe_id = ?", aid) == 1


def test_an_older_log_is_compared_with_the_flight_before_it(
    tmp_path: Path, pack: Pack, store: Store, cache: ColumnCache
) -> None:
    aid = airframe(store, pack)
    run(
        store,
        pack,
        cache,
        [variant(tmp_path, "a", system_id="board-A")],
        {"f1": {"airframe_id": aid}},
    )
    newer = variant(tmp_path, "b", system_id="board-B", STAT_BOOTCNT=1234)
    batch, _, (r,) = run(store, pack, cache, [newer], {"f1": {"airframe_id": aid}})
    (fid,) = r.flights
    row = store.get("flight", fid)
    assert row is not None and row["boot_utc_us"] is not None
    store.update("flight", fid, row["version"], {"boot_utc_us": row["boot_utc_us"] + DAY_US})
    builds = count(store, "SELECT count(*) FROM build_version WHERE airframe_id = ?", aid)
    # a log of board A from before board B flew, found later, with other tuning
    older = variant(tmp_path, "c", system_id="board-A", STAT_BOOTCNT=1235, ATC_RAT_RLL_P=0.2)
    _, (p,), (r,) = run(store, pack, cache, [older], {"f1": {"airframe_id": aid}})
    assert (p.status, p.how, p.airframe_id) == ("ready", "board", aid)
    assert r.status == "imported"
    assert count(store, "SELECT count(*) FROM build_version WHERE airframe_id = ?", aid) == builds
    current = store.get("airframe", aid)
    assert current is not None and current["board_id_hash"] == batch[0].board_id_hash
    assert count(store, "SELECT count(*) FROM change_set WHERE before_flight_id = ?", fid) == 0


def test_a_file_gone_before_its_scan_is_its_own_error(
    pack: Pack, store: Store, tmp_path: Path
) -> None:
    gone, ok = scan(store, pack, KEY, [tmp_path / "gone.bin", QUAD.with_suffix(".bin")])
    assert "FileNotFoundError" in (gone.error or "")
    assert ok.error is None and len(ok.flights) == 1


def test_every_boot_s_system_id_is_found(tmp_path: Path) -> None:
    b = LogBuilder()
    b.fmt(0x84, "VER", "QB", "TimeUS,BT")
    b.fmt(0x85, "MSG", "QZ", "TimeUS,Message")
    for board in ("SECRET-1", "SECRET-2"):
        b.write("VER", 1, 10)
        b.write("MSG", 2, f"CubeOrange {board}".encode())
        b.write("MSG", 3, b"ArduCopter V4.7.0")
    path = tmp_path / "boots.bin"
    path.write_bytes(b.bytes())
    with read_dataflash(path) as df:
        rows = system_id_rows(df)
        found = [text(df.rows(f)["Message"][k].tobytes()) for f, k in rows]
        assert system_id_row(df) == rows[0]
    assert found == ["CubeOrange SECRET-1", "CubeOrange SECRET-2"]


# --- over RPC -------------------------------------------------------------------------


def test_import_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import Output, call

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys)
    platform = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=platform["id"], name="Q-1")["result"]
    paths = [str(QUAD.with_suffix(e)) for e in (".bin", ".tlog")]
    scanned = call(server, out, 4, "import.scan", paths=paths)["result"]
    files = {f["id"]: f for f in scanned["files"]}
    assert files["f1"]["kind"] == "dataflash" and files["f1"]["flights"][0]["known"] is False
    assert files["f1"]["flights"][0]["arm_us"] > 0  # take-off is boot UTC plus this
    assert files["f2"]["proposal"]["status"] == "ready"
    assert "bid1-" not in json.dumps(scanned)  # the board's hash stays in the engine
    done = call(
        server,
        out,
        5,
        "import.commit",
        batch=scanned["batch"],
        decisions=[
            {
                "log": "f1",
                "airframe_id": af["id"],
                "packs": ["P-1"],
                "changes": [{"part": "motors", "reason_kind": "upgrade"}],
            }
        ],
    )["result"]
    assert [d["status"] for d in done] == ["imported", "linked"]
    again = call(server, out, 6, "import.commit", batch=scanned["batch"], decisions=[])
    assert again["error"]["code"] == -32003  # a batch is committed once
    bad = call(server, out, 7, "import.scan", paths=["/nonexistent.bin"])
    assert bad["error"]["code"] == -32003
    odd = call(
        server,
        out,
        8,
        "import.commit",
        batch="x",
        decisions=[{"log": "f1", "changes": [{"part": "x", "reason_kind": "whim"}]}],
    )
    assert odd["error"]["code"] in (-32003, -32602)
    assert call(server, out, 9, "import.held")["result"] == []
    call(server, out, 10, "engine.shutdown")


def test_a_batch_committed_twice_at_once_is_stored_once(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import Output, call

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    keys = {k: secrets.token_hex(32) for k in ("database", "cache", "positions", "board_id")}
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys)
    platform = call(server, out, 2, "platform.create", name="Quad", vehicle="copter")["result"]
    af = call(server, out, 3, "airframe.create", platform_id=platform["id"], name="Q-1")["result"]
    batch = call(server, out, 4, "import.scan", paths=[str(QUAD.with_suffix(".bin"))])["result"]
    params = {"batch": batch["batch"], "decisions": [{"log": "f1", "airframe_id": af["id"]}]}
    for rid in (5, 6):
        server.handle_line(
            json.dumps({"jsonrpc": "2.0", "id": rid, "method": "import.commit", "params": params})
        )
    first, second = sorted((out.reply(5), out.reply(6)), key=lambda r: "error" in r)
    assert [r["status"] for r in first["result"]] == ["imported"]
    assert second["error"]["code"] == -32003
    call(server, out, 7, "engine.shutdown")
