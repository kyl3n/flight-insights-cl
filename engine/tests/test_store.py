"""The local store: encrypted database, encrypted column cache, registration."""

from __future__ import annotations

import contextlib
import secrets
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from flight_insights.dataflash import read_dataflash
from flight_insights.store import (
    CacheError,
    ClockError,
    ColumnCache,
    HybridClock,
    Stamp,
    StoreError,
    StoreKeys,
    is_position,
    open_store,
    register_source,
    short_code,
)
from flight_insights.store.cache import schema_fields
from flight_insights.store.db import migrations
from flight_insights.tlog import read_tlog

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
LOG = FIXTURES / "copter-quad-hover.bin"
PLANE_TLOG = FIXTURES / "plane-circuit.tlog"
LAPTOP = "3f9a2c" + "0" * 26
OTHER_LAPTOP = "a" * 32
ENTITIES = {
    "laptop",
    "platform",
    "airframe",
    "build_version",
    "component",
    "battery_pack",
    "ground_station",
    "ground_station_version",
    "source_file",
    "flight",
    "param_snapshot",
    "change_set",
}


@pytest.fixture
def keys() -> StoreKeys:
    return StoreKeys.generate()


# --- keys ---------------------------------------------------------------------------


def test_keys_never_show_in_repr(keys: StoreKeys) -> None:
    text = repr(keys) + str(keys)
    for key in (keys.database, keys.cache, keys.positions):
        assert key.hex() not in text and repr(key) not in text


def test_keys_must_be_full_length_and_distinct() -> None:
    k = secrets.token_bytes(32)
    with pytest.raises(ValueError):
        StoreKeys(k[:16], secrets.token_bytes(32), secrets.token_bytes(32))
    with pytest.raises(ValueError):
        StoreKeys(k, k, secrets.token_bytes(32))


def test_laptop_short_code() -> None:
    assert short_code(LAPTOP) == "LT-3F9A2C"
    with pytest.raises(ValueError):
        short_code("LT-3F9A2C")


# --- clock --------------------------------------------------------------------------


def test_clock_increases_when_the_wall_clock_steps_back() -> None:
    wall = iter([1000, 1000, 999, 500, 1001, 1001])
    clock = HybridClock(LAPTOP, wall=lambda: next(wall))
    stamps = [clock.now() for _ in range(6)]
    assert stamps == sorted(stamps) and len(set(stamps)) == 6
    assert [Stamp.parse(s).ms for s in stamps] == [1000, 1000, 1000, 1000, 1001, 1001]


def test_string_order_is_time_order() -> None:
    stamps = [
        Stamp(999, 5, OTHER_LAPTOP),
        Stamp(1000, 0, LAPTOP),
        Stamp(1000, 0x10, LAPTOP),
        Stamp(10**12, 0, LAPTOP),
    ]
    assert sorted(str(s) for s in stamps) == [str(s) for s in sorted(stamps)]
    assert all(Stamp.parse(str(s)) == s for s in stamps)


def test_a_received_stamp_puts_the_next_local_one_after_it() -> None:
    now = [1000]
    clock = HybridClock(LAPTOP, wall=lambda: now[0])
    remote = str(Stamp(5000, 7, OTHER_LAPTOP))
    clock.receive(remote)
    later = clock.now()
    assert later > remote and Stamp.parse(later) == Stamp(5000, 8, LAPTOP)
    now[0] = 6000
    assert Stamp.parse(clock.now()) == Stamp(6000, 0, LAPTOP)


def test_a_stamp_from_far_in_the_future_is_refused() -> None:
    clock = HybridClock(LAPTOP, wall=lambda: 1000)
    with pytest.raises(ClockError):
        clock.receive(str(Stamp(1000 + 25 * 3600 * 1000, 0, OTHER_LAPTOP)))
    with pytest.raises(ClockError):
        clock.receive("not a stamp")


# --- database -----------------------------------------------------------------------


def test_the_schema_has_every_phase_0_entity(tmp_path: Path, keys: StoreKeys) -> None:
    with open_store(tmp_path / "fleet.db", keys, LAPTOP, "Field laptop") as store:
        assert store.record_tables >= ENTITIES
        laptop = store.get("laptop", LAPTOP)
        assert laptop is not None and laptop["name"] == "Field laptop"
        assert store.applied_migrations == [n for n, _, _ in migrations()]


def test_migrations_apply_once(tmp_path: Path, keys: StoreKeys) -> None:
    path = tmp_path / "fleet.db"
    with open_store(path, keys, LAPTOP) as store:
        store.insert("platform", {"name": "X8 heavy lift", "vehicle": "copter"})
    with open_store(path, keys, LAPTOP) as store:
        assert store.applied_migrations == []
        rows = store.conn.execute("SELECT number FROM schema_migrations").fetchall()
        assert [r[0] for r in rows] == [n for n, _, _ in migrations()]
        assert store.conn.execute("SELECT count(*) FROM platform").fetchone() == (1,)


def test_a_wrong_key_does_not_open_the_database(tmp_path: Path, keys: StoreKeys) -> None:
    path = tmp_path / "fleet.db"
    open_store(path, keys, LAPTOP).close()
    with pytest.raises(StoreError, match="key"):
        open_store(path, StoreKeys.generate(), LAPTOP)


def test_the_database_file_holds_no_plaintext(tmp_path: Path, keys: StoreKeys) -> None:
    path = tmp_path / "fleet.db"
    marker = "Airframe-" + secrets.token_hex(8)
    with open_store(path, keys, LAPTOP) as store:
        platform = store.insert("platform", {"name": "Survey quad", "vehicle": "copter"})
        store.insert("airframe", {"name": marker, "platform_id": platform})
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    for plain in (marker, "Survey quad", "airframe", "CREATE TABLE", "SQLite format 3", LAPTOP):
        assert plain.encode() not in data


def test_records_carry_sync_stamps_and_refuse_stale_updates(
    tmp_path: Path, keys: StoreKeys
) -> None:
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        pid = store.insert("platform", {"name": "Quad", "vehicle": "copter"})
        before = store.get("platform", pid)
        assert before is not None
        assert before["version"] == 1 and before["laptop_id"] == LAPTOP
        assert before["created_hlc"] == before["updated_hlc"]
        assert store.update("platform", pid, 1, {"name": "Quad mk2"}) == 2
        after = store.get("platform", pid)
        assert after is not None
        assert after["name"] == "Quad mk2" and after["updated_hlc"] > before["updated_hlc"]
        assert after["created_hlc"] == before["created_hlc"]
        with pytest.raises(StoreError, match="version 1"):
            store.update("platform", pid, 1, {"name": "lost change"})
        # callers cannot forge the stamps or the writer
        store.update("platform", pid, 2, {"laptop_id": OTHER_LAPTOP, "version": 99})
        final = store.get("platform", pid)
        assert final is not None and (final["laptop_id"], final["version"]) == (LAPTOP, 3)


def test_the_clock_resumes_after_the_newest_stored_stamp(tmp_path: Path, keys: StoreKeys) -> None:
    path = tmp_path / "fleet.db"
    with open_store(path, keys, LAPTOP) as store:
        future = str(Stamp(Stamp.parse(store.clock.now()).ms + 3_600_000, 0, LAPTOP))
        pid = store.insert("platform", {"name": "Quad", "vehicle": "copter"})
        store.conn.execute("UPDATE platform SET updated_hlc = ? WHERE id = ?", (future, pid))
    with open_store(path, keys, LAPTOP) as store:
        assert store.clock.now() > future


def test_a_failed_transaction_leaves_nothing(tmp_path: Path, keys: StoreKeys) -> None:
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        with pytest.raises(RuntimeError), store.transaction():
            store.insert("platform", {"name": "Quad", "vehicle": "copter"})
            raise RuntimeError("crash mid-import")
        assert store.conn.execute("SELECT count(*) FROM platform").fetchone() == (0,)


def test_threads_share_the_store_one_statement_at_a_time(tmp_path: Path, keys: StoreKeys) -> None:
    """The engine's request threads share one connection. Reads stepping at the same
    time as other reads and writes once corrupted its statement cache (a KeyError naming
    the SQL); every statement now runs under the store's lock."""
    import threading

    errors: list[BaseException] = []
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        start = threading.Barrier(8)

        def work(n: int) -> None:
            try:
                start.wait()
                for k in range(150):
                    if n % 2:
                        store.insert("platform", {"name": f"P{n}-{k}", "vehicle": "copter"})
                    # distinct statements, so the connection's statement cache (128)
                    # keeps evicting, as the engine's many queries do
                    rows = store.conn.execute(
                        f"SELECT id, name, {n * 1000 + k} FROM platform WHERE name LIKE "
                        f"'P{n % 3}%' ORDER BY name"
                    ).fetchall()
                    assert all(len(r) == 3 for r in rows)
                    for (count,) in store.conn.execute("SELECT count(*) FROM platform"):
                        assert count >= 0
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert store.conn.execute("SELECT count(*) FROM platform").fetchone() == (4 * 150,)


def test_no_thread_reads_another_threads_open_transaction(tmp_path: Path, keys: StoreKeys) -> None:
    import threading

    seen: list[int] = []
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        opened, done = threading.Event(), threading.Event()

        def writer() -> None:
            with contextlib.suppress(RuntimeError), store.transaction():
                store.insert("platform", {"name": "never kept", "vehicle": "copter"})
                opened.set()
                done.wait(0.3)
                raise RuntimeError("rolled back")

        def reader() -> None:
            opened.wait()
            seen.append(store.conn.execute("SELECT count(*) FROM platform").fetchone()[0])
            done.set()

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert seen == [0]  # it waited for the rollback, and never saw the row


def test_foreign_keys_and_checks_are_enforced(tmp_path: Path, keys: StoreKeys) -> None:
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            store.insert("airframe", {"name": "orphan", "platform_id": "nope"})
        with pytest.raises(Exception, match="CHECK"):
            store.insert("platform", {"name": "Boat", "vehicle": "rover"})
        with pytest.raises(StoreError):
            store.insert("sqlite_schema", {"name": "x"})


# --- registration -------------------------------------------------------------------


def test_registering_the_same_content_twice_gives_one_row(tmp_path: Path, keys: StoreKeys) -> None:
    copy = tmp_path / "renamed.bin"
    copy.write_bytes(LOG.read_bytes())
    changed = tmp_path / "changed.bin"
    changed.write_bytes(LOG.read_bytes() + b"\x00")
    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        first = register_source(store, LOG, "dataflash")
        again = register_source(store, copy, "dataflash")
        other = register_source(store, changed, "dataflash")
        assert not first.duplicate and again.duplicate and not other.duplicate
        assert again.id == first.id and other.id != first.id
        assert store.conn.execute("SELECT count(*) FROM source_file").fetchone() == (2,)


# --- column cache -------------------------------------------------------------------


def ipc_bytes(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table.combine_chunks())
    return sink.getvalue().to_pybytes()


def same(a: pa.Table, b: pa.Table) -> bool:
    """Bit for bit, metadata and NaNs included (Table.equals treats NaN as unequal)."""
    return ipc_bytes(a) == ipc_bytes(b)


def positions_in(table: pa.Table) -> list[str]:
    return [f.name for f in schema_fields(table.schema) if is_position(f)]


def test_the_cache_round_trips_decoded_tables(tmp_path: Path, keys: StoreKeys) -> None:
    cache = ColumnCache(tmp_path / "cache", keys)
    with read_dataflash(LOG) as df:
        for name in ("GPS", "POS", "IMU", "PARM"):
            table = df.table(name)
            cache.write("flight1", name, df.iter_batches(name, rows=500))
            back = cache.read("flight1", name, positions=True)
            assert same(back, table)
    assert cache.names("flight1") == ["GPS", "IMU", "PARM", "POS"]


def test_position_columns_stay_out_of_analysis_reads(tmp_path: Path, keys: StoreKeys) -> None:
    cache = ColumnCache(tmp_path / "cache", keys)
    with read_dataflash(LOG) as df:
        gps = df.table("GPS")
    hidden = positions_in(gps)
    assert set(hidden) >= {"Lat", "Lng"}
    cache.write_table("flight1", "GPS", gps)
    analysis = cache.read("flight1", "GPS")
    assert analysis.column_names == [c for c in gps.column_names if c not in hidden]
    assert same(analysis, gps.drop_columns(hidden))
    with pytest.raises(CacheError, match="positions"):
        cache.read("flight1", "GPS", columns=["TimeUS", "Lat"])


def test_position_columns_need_the_positions_key(tmp_path: Path, keys: StoreKeys) -> None:
    with read_dataflash(LOG) as df:
        gps = df.table("GPS")
    ColumnCache(tmp_path, keys).write_table("f", "GPS", gps)
    stranger = StoreKeys(keys.database, keys.cache, secrets.token_bytes(32))
    other = ColumnCache(tmp_path, stranger)
    assert other.read("f", "GPS").num_rows == gps.num_rows
    with pytest.raises(CacheError, match="positions key does not open"):
        other.read("f", "GPS", positions=True)
    with pytest.raises(CacheError, match="cache key does not open"):
        ColumnCache(tmp_path, StoreKeys.generate()).read("f", "GPS")


def test_tlog_positions_go_under_the_positions_key(tmp_path: Path, keys: StoreKeys) -> None:
    tlog = read_tlog(PLANE_TLOG)
    # Mission rows hold a position only for some commands; the whole column is hidden.
    mission = pa.Table.from_arrays(
        [pa.array([0, 1], pa.uint16()), pa.array([1, 2], pa.int32())],
        schema=pa.schema(
            [
                pa.field("seq", pa.uint16()),
                pa.field("x", pa.int32(), metadata={"position": "by_command"}),
            ]
        ),
    )
    tables = {"MISSION_ITEM_INT": mission} | {
        n: tlog.tables[n] for n in ("GLOBAL_POSITION_INT", "HOME_POSITION")
    }
    cache = ColumnCache(tmp_path, keys)
    for name, table in tables.items():
        hidden = positions_in(table)
        assert hidden, name
        cache.write_table("f", name, table)
        assert not set(hidden) & set(cache.read("f", name).column_names)
        assert same(cache.read("f", name, positions=True), table)


def test_cache_files_hold_no_plaintext(tmp_path: Path, keys: StoreKeys) -> None:
    """Uncompressed, so a plain-text column would show up byte for byte."""
    cache = ColumnCache(tmp_path, keys, compression="none")
    with read_dataflash(LOG) as df:
        gps = df.table("GPS")
        msg = df.table("MSG")
    path = cache.write_table("f", "GPS", gps)
    data = path.read_bytes()
    # Ciphertext is random, so a short pattern turns up by chance (a 3-byte one in
    # about 1% of runs): each search is 8 bytes or longer. Plaintext values would sit
    # side by side, as consecutive rows or as a dictionary in first-seen order.
    lat = gps.column("Lat").to_numpy().astype("<i4")
    first_seen = lat[np.sort(np.unique(lat, return_index=True)[1])]
    for values in (lat, first_seen):
        for i in range(min(50, len(values) - 1)):
            assert values[i : i + 2].tobytes() not in data
    for name in (b"Lat", b"TimeUS"):  # a plaintext Thrift footer writes length, then name
        assert bytes([len(name)]) + name not in data
    assert b"ARROW:schema" not in data
    text = cache.write_table("f", "MSG", msg).read_bytes()
    first = bytes(msg.column("Message")[0].as_py()).rstrip(b"\x00")
    assert len(first) > 8 and first not in text


def test_cache_names_cannot_escape_the_cache(tmp_path: Path, keys: StoreKeys) -> None:
    cache = ColumnCache(tmp_path, keys)
    for flight, name in (("../x", "GPS"), ("f", "../GPS"), ("f", ".hidden"), ("", "GPS")):
        with pytest.raises(CacheError):
            cache.path(flight, name)


def test_a_type_name_no_file_can_have_is_reported_not_cacheable(
    tmp_path: Path, keys: StoreKeys
) -> None:
    """A corrupted FMT can name a type anything: the importer asks first and skips it."""
    cache = ColumnCache(tmp_path, keys)
    assert cache.cacheable("GPS") and cache.cacheable("GPS.2")
    for name in ("", "\\x01AB", "G/S", ".GPS"):
        assert not cache.cacheable(name), name
        with pytest.raises(CacheError):
            cache.path("f", name)


def test_a_failed_write_leaves_no_file(tmp_path: Path, keys: StoreKeys) -> None:
    cache = ColumnCache(tmp_path, keys)
    a = pa.Table.from_arrays([pa.array([1], pa.int32())], names=["x"]).to_batches()[0]
    b = pa.Table.from_arrays([pa.array([1], pa.int32())], names=["y"]).to_batches()[0]
    with pytest.raises(CacheError, match="schemas"):
        cache.write("f", "T", [a, b])
    assert not any(tmp_path.rglob("*.parquet*")) and not any(tmp_path.rglob(".*partial"))
    cache.write("f", "T", [a])
    cache.evict("f")
    assert cache.names("f") == []


def test_threads_share_the_store_safely(tmp_path: Path, keys: StoreKeys) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:

        def work(n: int) -> None:
            with store.transaction():
                pid = store.insert("platform", {"name": f"P{n}", "vehicle": "copter"})
                store.insert("airframe", {"name": f"A{n}", "platform_id": pid})

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(work, range(200)))
        assert store.conn.execute("SELECT count(*) FROM airframe").fetchone() == (200,)


def test_concurrent_inserts_never_share_a_stamp(tmp_path: Path, keys: StoreKeys) -> None:
    import threading

    both = threading.Barrier(2, timeout=0.5)

    class RacyClock(HybridClock):
        """Reads its state, then writes it back: two callers at once get one stamp."""

        def __init__(self) -> None:
            super().__init__(LAPTOP)
            self.last = super().now()

        def now(self) -> str:
            seen = self.last
            with contextlib.suppress(threading.BrokenBarrierError):
                both.wait()  # meets the other thread only if both read unlocked
            self.last = super().now()
            return seen

    with open_store(tmp_path / "fleet.db", keys, LAPTOP) as store:
        store.clock = RacyClock()
        threads = [
            threading.Thread(
                target=store.insert, args=("platform", {"name": n, "vehicle": "copter"})
            )
            for n in ("A", "B")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps = store.conn.execute("SELECT created_hlc FROM platform").fetchall()
    assert len(stamps) == 2 and len(set(stamps)) == 2
