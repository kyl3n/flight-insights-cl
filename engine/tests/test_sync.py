"""Sync's engine half: two laptops' stores exchange plaintext objects the way the shell
will hand them over, and must end up holding the same records. No crypto and no drive
here; the shell's checks come in its own tests."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flight_insights.ai import record
from flight_insights.ai.accept import accept, results_for
from flight_insights.ai.packet import build
from flight_insights.analyzers.run import finding_keys, run_analysis
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import Pack, load_pack
from flight_insights.platforms import create_airframe, create_platform, load_schema
from flight_insights.recommend import flight_plan, plan_and_record, set_status
from flight_insights.store import Store, StoreKeys, open_store, synced
from flight_insights.store.cache import ColumnCache
from flight_insights.sync import (
    apply_records,
    apply_source,
    conflicts,
    digest,
    export,
    mark_sent,
    outbox,
    quarantine,
    resolve,
    status,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
QUAD = FIXTURES / "copter-quad-hover.bin"
BOARD_KEY = bytes(range(32))  # the fleet's, the same on both (from the fleet key in 18b)
A, B, C = "a" * 32, "b" * 32, "c" * 32
BOOKKEEPING = {"version", "updated_hlc", "laptop_id"}  # each laptop's own count of edits


class Laptop:
    def __init__(self, root: Path, laptop_id: str) -> None:
        keys = StoreKeys.generate()
        self.store: Store = open_store(root / f"{laptop_id[:1]}.db", keys, laptop_id)
        self.cache = ColumnCache(root / f"{laptop_id[:1]}-cache", keys)
        self.id = laptop_id
        self.sent: list[tuple[str, dict[str, Any]]] = []  # (object key, object), as written

    def send(self) -> list[tuple[str, dict[str, Any]]]:
        """What the shell would upload: each object in the outbox, marked sent."""
        new: list[tuple[str, dict[str, Any]]] = []
        for obj in outbox(self.store, 500):
            body = json.dumps(obj["payload"], sort_keys=True).encode()
            key = hashlib.sha256(body).hexdigest()
            mark_sent(self.store, obj["sequence"], key)
            new.append((key, obj))
        self.sent += new
        return new

    def receive(self, pack: Pack, objects: list[tuple[str, dict[str, Any]]]) -> list[str]:
        states: list[str] = []
        for key, obj in objects:
            if obj["kind"] == "records":
                got = apply_records(self.store, key, obj["laptop"], obj["sequence"], obj["payload"])
            else:
                manifest = {k: v for k, v in obj["payload"].items() if k != "path"}
                got = apply_source(
                    self.store, self.cache, pack, key, obj["laptop"], obj["sequence"], manifest,
                    Path(obj["payload"]["path"]),
                )  # fmt: skip
            states.append(got.state)
        return states

    def close(self) -> None:
        self.store.close()


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture
def laptops(tmp_path: Path, pack: Pack) -> Iterator[tuple[Laptop, Laptop]]:
    """Two laptops that have met: each holds the other's own record."""
    a, b = Laptop(tmp_path, A), Laptop(tmp_path, B)
    sync(pack, a, b)
    yield a, b
    a.close()
    b.close()


def sync(pack: Pack, *laptops: Laptop) -> None:
    """Every laptop sends, then every laptop receives everything the others sent."""
    for lt in laptops:
        lt.send()
    for lt in laptops:
        for other in laptops:
            if other is not lt:
                lt.receive(pack, other.sent)


def state(lt: Laptop) -> dict[str, Any]:
    """Everything that syncs, by table and key, without each laptop's bookkeeping.
    The engine's own digest must agree with it, laptop to laptop."""
    out: dict[str, Any] = {}
    conn = lt.store.conn
    for table in [
        *synced.RECORDS,
        *synced.LINKS,
        *(c for k in synced.CHILDREN.values() for c in k),
    ]:
        cur = conn.execute(f"SELECT * FROM {table}")
        names = [d[0] for d in cur.description]
        drop = BOOKKEEPING | synced.LOCAL_FIELDS.get(table, frozenset())
        rows = [
            json.dumps({n: v for n, v in zip(names, r, strict=True) if n not in drop},
                       sort_keys=True, default=lambda b: bytes(b).hex())
            for r in cur.fetchall()
        ]  # fmt: skip
        out[table] = sorted(rows)
    return out


def import_quad(lt: Laptop, pack: Pack, airframe_id: str, path: Path = QUAD) -> list[str]:
    batch = scan(lt.store, pack, BOARD_KEY, [path])
    proposals = propose(lt.store, pack, batch)
    (result,) = commit(lt.store, pack, lt.cache, batch, proposals,
                       [Decision(batch[0].id, airframe_id=airframe_id)])  # fmt: skip
    return result.flights


def quad_airframe(lt: Laptop, pack: Pack) -> str:
    platform = create_platform(lt.store, load_schema(pack), "Quad", "copter")
    return create_airframe(lt.store, platform, "Q-1")


# --- what syncs ---------------------------------------------------------------------


def test_every_table_is_classified_once(laptops: tuple[Laptop, Laptop]) -> None:
    a, _ = laptops
    children = [c for kids in synced.CHILDREN.values() for c in kids]
    groups = [*synced.RECORDS, *synced.LINKS, *children, *synced.LOCAL]
    assert len(groups) == len(set(groups)), "a table is in two groups"
    assert set(groups) == set(a.store.tables), set(a.store.tables) ^ set(groups)
    for table, fields in synced.LOCAL_FIELDS.items():
        assert table in synced.RECORDS
        assert fields <= {r[1] for r in a.store.conn.execute(f"PRAGMA table_info({table})")}


SRC = Path(__file__).resolve().parents[1] / "src" / "flight_insights"
WRITE = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _writes(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = node.value.upper().split()
            for i, w in enumerate(words[:-1]):
                if w in WRITE:
                    target = words[i + 1] if words[i + 1] not in ("INTO", "OR", "FROM") else None
                    rest = words[i + 1 :]
                    for j, x in enumerate(rest[:-1]):
                        if x in ("INTO", "FROM") or (w == "UPDATE" and j == 0):
                            target = rest[j + 1] if x in ("INTO", "FROM") else x
                            break
                    if target:
                        yield node.lineno, target.lower().strip("(")


def test_synced_tables_are_written_only_through_the_store() -> None:
    """A raw write to a synced table would never be captured, and so never sent."""
    allowed = {SRC / "store" / "db.py"}
    allowed |= set((SRC / "sync").glob("*.py"))
    allowed.add(SRC / "history" / "persist.py")  # a snapshot's values, with the snapshot
    watched = {*synced.RECORDS, *synced.LINKS}
    found: list[str] = []
    for path in SRC.rglob("*.py"):
        if path in allowed:
            continue
        for line, table in _writes(ast.parse(path.read_text(encoding="utf-8"))):
            if table in watched:
                found.append(f"{path.relative_to(SRC)}:{line} writes {table}")
    assert not found, found


def _rename(lt: Laptop, table: str, rid: str, **values: Any) -> None:
    row = lt.store.get(table, rid)
    assert row is not None
    lt.store.update(table, rid, row["version"], values)


# --- two laptops ----------------------------------------------------------------------


def test_an_import_reaches_the_other_laptop_whole(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    airframe = quad_airframe(a, pack)
    (flight,) = import_quad(a, pack, airframe)
    sent = a.send()
    kinds = [o["kind"] for _, o in sent]
    assert kinds.count("source") == 1 and "records" in kinds
    assert all("path" not in json.dumps(o["payload"]) for _, o in sent if o["kind"] == "records")
    assert set(b.receive(pack, sent)) == {"applied"}
    assert state(a) == state(b)
    assert digest(a.store) == digest(b.store)
    key = b.store.conn.execute("SELECT flight_key FROM flight WHERE id = ?", (flight,)).fetchone()
    assert b.cache.names(key[0]) == a.cache.names(key[0]) != []
    # B recomputes the analysis from the log and finds the same things
    ra, rb = (run_analysis(lt.store, lt.cache, pack, flight) for lt in (a, b))
    assert ra["run_key"] == rb["run_key"]
    assert [f["key"] for f in ra["findings"]] == [f["key"] for f in rb["findings"]]
    assert [f["id"] for f in ra["findings"]] != [f["id"] for f in rb["findings"]]
    # and B sends nothing back: what it applied is not its own change
    assert b.send() == []


def test_applying_an_object_twice_changes_nothing(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    quad_airframe(a, pack)
    sent = a.send()
    b.receive(pack, sent)
    before = state(b)
    key, obj = sent[0]
    again = apply_records(b.store, key, A, obj["sequence"], obj["payload"])
    assert (again.state, again.reason) == ("applied", "already applied")
    assert state(b) == before
    # the same sequence number under another key is refused, never applied
    other = apply_records(b.store, "f" * 64, A, obj["sequence"], obj["payload"])
    assert other.state == "quarantined"


def test_an_object_waits_for_the_records_it_refers_to(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    platform = create_platform(a.store, load_schema(pack), "Quad", "copter")
    first = a.send()
    create_airframe(a.store, platform, "Q-1")
    second = a.send()
    assert b.receive(pack, second) == ["waiting"]
    assert b.store.conn.execute("SELECT count(*) FROM airframe").fetchone()[0] == 0
    assert b.receive(pack, first) == ["applied"]
    assert b.receive(pack, second) == ["applied"]
    assert state(a) == state(b)


def test_status_shows_gaps_until_they_fill(laptops: tuple[Laptop, Laptop], pack: Pack) -> None:
    a, b = laptops
    create_platform(a.store, load_schema(pack), "Quad", "copter")
    first = a.send()
    create_platform(a.store, load_schema(pack), "Hexa", "copter")
    second = a.send()
    assert b.receive(pack, second) == ["applied"]  # it needs nothing from the first
    assert status(b.store)["laptops"] == [{"laptop": A, "latest": 3, "objects": 2, "gaps": [2]}]
    b.receive(pack, first)
    (row,) = status(b.store)["laptops"]
    assert (row["latest"], row["gaps"]) == (3, [])
    assert status(a.store)["waiting_to_send"] == 0


# --- field merges -----------------------------------------------------------------------


def test_a_change_made_after_seeing_the_other_is_no_conflict(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    airframe = quad_airframe(a, pack)
    sync(pack, a, b)
    _rename(b, "airframe", airframe, name="Q-1 (B)")
    sync(pack, a, b)
    _rename(a, "airframe", airframe, name="Q-1 (A after B)")
    sync(pack, a, b)
    assert a.store.get("airframe", airframe)["name"] == "Q-1 (A after B)"  # type: ignore[index]
    assert state(a) == state(b)
    assert conflicts(a.store) == conflicts(b.store) == []


def test_concurrent_changes_keep_both_values_and_agree_on_one(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    airframe = quad_airframe(a, pack)
    sync(pack, a, b)
    _rename(a, "airframe", airframe, name="from A")
    _rename(b, "airframe", airframe, name="from B")  # later: B's clock is ahead
    assert digest(a.store) != digest(b.store)
    sync(pack, a, b)
    assert digest(a.store) == digest(b.store)
    assert state(a) == state(b)
    assert a.store.get("airframe", airframe)["name"] == "from B"  # type: ignore[index]
    for lt in (a, b):
        (c,) = conflicts(lt.store)
        assert (c["tbl"], c["field"], c["kept"], c["other"]) == (
            "airframe", "name", "from B", "from A"
        )  # fmt: skip


def test_keeping_the_other_value_settles_the_conflict_everywhere(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    airframe = quad_airframe(a, pack)
    sync(pack, a, b)
    _rename(a, "airframe", airframe, name="from A")
    _rename(b, "airframe", airframe, name="from B")
    sync(pack, a, b)
    (c,) = conflicts(a.store)
    assert (c["kept"], c["other"]) == ("from B", "from A")
    resolve(a.store, c["id"], "other")
    sync(pack, a, b)
    assert state(a) == state(b)
    got = b.store.get("airframe", airframe)
    assert got is not None and got["name"] == "from A"
    assert conflicts(a.store) == conflicts(b.store) == []
    with pytest.raises(ValueError):
        resolve(a.store, c["id"], "kept")  # already settled


def test_different_fields_of_one_record_both_survive(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    platform = create_platform(a.store, load_schema(pack), "Quad", "copter")
    sync(pack, a, b)
    _rename(a, "platform", platform, name="Quad X")
    _rename(b, "platform", platform, targets_json='{"hover_throttle": 0.35}')
    sync(pack, a, b)
    assert state(a) == state(b)
    got = a.store.get("platform", platform)
    assert got is not None
    assert (got["name"], got["targets_json"]) == ("Quad X", '{"hover_throttle": 0.35}')
    assert conflicts(a.store) == conflicts(b.store) == []


def test_the_same_value_set_on_both_is_no_conflict(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    platform = create_platform(a.store, load_schema(pack), "Quad", "copter")
    sync(pack, a, b)
    _rename(a, "platform", platform, name="Quad X")
    _rename(b, "platform", platform, name="Quad X")
    sync(pack, a, b)
    assert state(a) == state(b)
    assert conflicts(a.store) == conflicts(b.store) == []


# --- the same thing made on two laptops ---------------------------------------------------


def test_a_log_imported_on_both_laptops_becomes_one_flight(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    airframe = quad_airframe(a, pack)
    sync(pack, a, b)
    (fa,) = import_quad(a, pack, airframe)
    (fb,) = import_quad(b, pack, airframe)
    assert fa != fb
    sync(pack, a, b)
    sync(pack, a, b)  # the merge itself is sent on
    assert state(a) == state(b)
    for lt in (a, b):
        conn = lt.store.conn
        assert conn.execute("SELECT count(*) FROM flight").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM source_file").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM param_snapshot WHERE origin = 'log start'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT id FROM flight").fetchone()[0] == min(fa, fb)
        assert conflicts(lt.store) == []
    # the laptop whose copy went keeps its decoded columns under the same key
    (key,) = b.store.conn.execute("SELECT flight_key FROM flight").fetchone()
    assert b.cache.names(key)


def test_recommendations_made_on_both_laptops_are_kept_once(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    airframe = quad_airframe(a, pack)
    import_quad(a, pack, airframe)
    sync(pack, a, b)
    (flight,) = (r[0] for r in a.store.conn.execute("SELECT id FROM flight"))
    pa = plan_and_record(a.store, a.cache, pack, flight)
    pb = plan_and_record(b.store, b.cache, pack, flight)
    kept = [r for r in pa["recommendations"] if r.get("id")]
    assert kept, "the fixture's plan proposes something"
    first = kept[0]
    set_status(a.store, first["id"], "accepted", first["record_version"])
    sync(pack, a, b)
    sync(pack, a, b)
    assert state(a) == state(b)
    count = "SELECT count(*) FROM recommendation"
    assert a.store.conn.execute(count).fetchone()[0] == len(kept)
    # A's decision stands against B's copy, which no one had touched
    code, param = first["source"].removeprefix("finding:"), first["canonical"]
    status_now = b.store.conn.execute(
        "SELECT status FROM recommendation WHERE finding_code = ? AND param = ?", (code, param)
    ).fetchone()[0]
    assert status_now == "accepted"
    assert conflicts(b.store) == []
    assert len([r for r in pb["recommendations"] if r.get("id")]) == len(kept)


@pytest.mark.parametrize("order", ["abc", "acb", "bac", "cab", "cba"])
def test_twins_merge_the_same_whatever_order_the_laptops_meet_in(
    tmp_path: Path, pack: Pack, order: str
) -> None:
    # One log imported on two laptops onto different airframes, and planned on a third
    # that has seen only one copy: one flight once the copies meet, and the same
    # recommendations on every laptop, each on the airframe its flight is on. Found by
    # the soak (seed 1, 30 rounds, 4 laptops): a starting value merged into the kept
    # record was later compared with the kept record's own clock.
    lts = {k: Laptop(tmp_path, i) for k, i in zip("abc", (A, B, C), strict=True)}
    a, b, c = lts["a"], lts["b"], lts["c"]
    sync(pack, a, b, c)
    on_a, on_b = quad_airframe(a, pack), quad_airframe(b, pack)
    sync(pack, a, b, c)
    (fa,) = import_quad(a, pack, on_a)
    (fb,) = import_quad(b, pack, on_b)
    a.send()
    c.receive(pack, a.sent)
    for lt, flight in ((a, fa), (b, fb), (c, fa)):
        plan_and_record(lt.store, lt.cache, pack, flight)
    for _ in range(3):
        sync(pack, *(lts[k] for k in order))
    assert state(a) == state(b) == state(c)
    for lt in lts.values():
        (airframe,) = lt.store.conn.execute("SELECT airframe_id FROM flight").fetchone()
        rows = lt.store.conn.execute("SELECT DISTINCT airframe_id FROM recommendation")
        assert [r[0] for r in rows] == [airframe]
        lt.close()


# --- refusing ---------------------------------------------------------------------------


NONSENSE: list[tuple[Any, str]] = [
    ([], "not a records object"),
    ({"format": "x"}, "not a records object this app reads"),
    ({"format": "fi.sync.records/1", "laptop": B, "entries": []}, "written by a different"),
    ({"format": "fi.sync.records/1", "laptop": A, "entries": [{"op": "put", "t": "finding"}]},
     "does not sync"),
    ({"format": "fi.sync.records/1", "laptop": A, "entries": [{"op": "drop", "t": "flight"}]},
     "unknown kind"),
    ({"format": "fi.sync.records/1", "laptop": A,
      "entries": [{"op": "set", "t": "platform", "id": "x", "fields": {}}]},
     "not yet received"),
    ({"format": "fi.sync.records/1", "laptop": A,
      "entries": [{"op": "link", "t": "flight_battery",
                   "row": {"flight_id) VALUES (1); DROP TABLE flight; --": 1}}]},
     "does not know yet"),
]  # fmt: skip


@pytest.mark.parametrize(("payload", "reason"), NONSENSE)
def test_nonsense_is_quarantined_with_a_reason_and_no_values(
    laptops: tuple[Laptop, Laptop], payload: Any, reason: str
) -> None:
    _, b = laptops
    got = apply_records(b.store, "e" * 64, A, 50, payload)
    assert reason in str(got.reason)
    if got.state == "quarantined":
        (row,) = status(b.store)["quarantine"]
        assert row["reason"] == got.reason
        assert "x" not in row["reason"].split()  # never a value from the object


def test_a_change_to_a_record_s_identity_is_quarantined(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    a, b = laptops
    platform = create_platform(a.store, load_schema(pack), "Quad", "copter")
    sync(pack, a, b)
    change = {"created_hlc": {"v": "0", "hlc": "z", "base": None}}
    entry = {"op": "set", "t": "platform", "id": platform, "fields": change}
    bad = {"format": "fi.sync.records/1", "laptop": A, "schema": 14, "entries": [entry]}
    got = apply_records(b.store, "d" * 64, A, 50, bad)
    assert got.state == "quarantined"
    assert "created_hlc" in str(got.reason)


def test_an_object_from_a_newer_app_waits(laptops: tuple[Laptop, Laptop]) -> None:
    _, b = laptops
    got = apply_records(
        b.store, "c" * 64, A, 50,
        {"format": "fi.sync.records/1", "laptop": A, "schema": 999, "entries": []},
    )  # fmt: skip
    assert got.state == "waiting"


def test_a_source_that_is_not_the_file_it_names_is_quarantined(
    laptops: tuple[Laptop, Laptop], pack: Pack, tmp_path: Path
) -> None:
    _, b = laptops
    other = tmp_path / "other.bin"
    other.write_bytes(b"not the log")
    manifest = {"format": "fi.sync.source/1", "sha256": "0" * 64, "kind": "dataflash", "size": 1}
    got = apply_source(b.store, b.cache, pack, "b" * 64, A, 50, manifest, other)
    assert got.state == "quarantined"


def test_the_shell_s_own_refusals_are_kept(laptops: tuple[Laptop, Laptop]) -> None:
    _, b = laptops
    quarantine(b.store, "a" * 64, A, 3, "the signature does not hold")
    (row,) = status(b.store)["quarantine"]
    assert (row["laptop"], row["sequence"], row["reason"]) == (A, 3, "the signature does not hold")


# --- portable references ------------------------------------------------------------------


def test_finding_keys_do_not_depend_on_ids_or_numbers() -> None:
    one = finding_keys([("x1", "vibe.high", ["vibe:hover"]), ("x2", "vibe.high", ["vibe:hover"])])
    two = finding_keys([("y1", "vibe.high", ["vibe:hover"]), ("y2", "vibe.high", ["vibe:hover"])])
    assert list(one.values()) == list(two.values())
    assert len(set(one.values())) == 2
    assert all(k.startswith("vibe.high#") for k in one.values())


def test_an_ai_reply_made_on_one_laptop_reads_on_the_other(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    """AI results cannot be made again, so they sync; their references name findings by
    key, and each laptop finds its own copy of the finding they mean."""
    a, b = laptops
    airframe = quad_airframe(a, pack)
    (flight,) = import_quad(a, pack, airframe)
    built = build(a.store, a.cache, pack, flight)
    text = record.packet_text(built.packet)
    kept = record.record_packet(a.store, flight, "analysis", text, [], run_id=built.run_id,
                                run_key=built.run_key, refs=built.refs)  # fmt: skip
    record.record_outcome(a.store, kept["id"], [], sent=True)
    ref = built.packet["findings"][0]["ref"]
    reply = {"summary": "s", "root_causes": [{"title": "t", "explanation": "e", "refs": [ref]}],
             "recommendations": [{"param": "INS_HNTCH_BW", "value": 35, "confidence": "medium",
                                  "reason": "Widen the notch.", "refs": [ref]}],
             "disagreements": [], "questions": [], "tests_to_fly": []}  # fmt: skip
    got = accept(a.store, pack, kept["id"], model="m", stop_reason="end_turn",
                 reply_text=json.dumps(reply))  # fmt: skip
    assert got["outcome"] == "accepted"
    sync(pack, a, b)
    assert state(a) == state(b)
    for lt in (a, b):
        run = run_analysis(lt.store, lt.cache, pack, flight)
        shown = results_for(lt.store, flight)
        assert shown is not None
        assert shown["refs"][ref] == run["findings"][0]["id"]  # this laptop's own finding
        plan = flight_plan(lt.store, lt.cache, pack, flight, run)
        ai = [r for r in plan.recommendations if any(x.origin == "ai" for x in r.sources)]
        assert ai, lt.id


# --- the engine over RPC ------------------------------------------------------------------


def test_sync_methods_over_rpc(tmp_path: Path, pack: Pack) -> None:
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    call(server, out, 1, "engine.unlock", laptop_id=LAPTOP, data_dir=str(tmp_path), keys=keys())
    objects = call(server, out, 2, "sync.outbox", limit=10)["result"]
    assert [o["kind"] for o in objects] == ["records"]  # this laptop's own record, once
    seq = objects[0]["sequence"]
    assert call(server, out, 3, "sync.sent", sequence=seq, object_key="ab" * 32)["result"] == {}
    assert call(server, out, 4, "sync.outbox")["result"] == []
    own = call(server, out, 5, "sync.apply", object_key="cd" * 32, laptop=LAPTOP, sequence=1,
               kind="records", payload=objects[0]["payload"])  # fmt: skip
    assert "never applied back" in own["error"]["message"]
    other = objects[0]["payload"] | {"laptop": A, "entries": []}
    got = call(server, out, 6, "sync.apply", object_key="ef" * 32, laptop=A, sequence=1,
               kind="records", payload=other)["result"]  # fmt: skip
    assert got == {"state": "applied", "reason": None}
    bad = call(server, out, 7, "sync.apply", object_key="NOT HEX", laptop=A, sequence=2,
               kind="records", payload=other)  # fmt: skip
    assert bad["error"]["code"] == -32602
    got = call(server, out, 8, "sync.quarantine", object_key="12" * 32, laptop=A, sequence=2,
               reason="the signature does not hold")["result"]  # fmt: skip
    assert got["state"] == "quarantined"
    st = call(server, out, 9, "sync.status")["result"]
    assert st["laptops"] == [{"laptop": A, "latest": 1, "objects": 1, "gaps": []}]
    assert len(st["quarantine"]) == 1
    assert call(server, out, 10, "sync.conflicts")["result"] == []
    keys = ["ef" * 32, "12" * 32, "34" * 32]
    assert call(server, out, 11, "sync.unknown", object_keys=keys)["result"] == ["34" * 32]
    assert call(server, out, 12, "sync.unknown", object_keys=["x"])["error"]["code"] == -32602


def test_export_with_nothing_pending_makes_no_object(laptops: tuple[Laptop, Laptop]) -> None:
    a, _ = laptops
    assert export(a.store) == []


def test_planning_a_flight_again_keeps_a_decided_recommendation(
    laptops: tuple[Laptop, Laptop], pack: Pack
) -> None:
    """Found by the soak: a rejected recommendation was proposed afresh when its flight
    was planned again, and the two records merged on other laptops but not on this one."""
    a, b = laptops
    airframe = quad_airframe(a, pack)
    (flight,) = import_quad(a, pack, airframe)
    plan = plan_and_record(a.store, a.cache, pack, flight)
    first = next(r for r in plan["recommendations"] if r.get("id"))
    set_status(a.store, first["id"], "rejected", first["record_version"])
    again = plan_and_record(a.store, a.cache, pack, flight)
    same = [r for r in again["recommendations"] if r.get("id") == first["id"]]
    assert same and same[0]["record_status"] == "rejected"
    count = "SELECT count(*) FROM recommendation"
    before = a.store.conn.execute(count).fetchone()[0]
    sync(pack, a, b)
    assert b.store.conn.execute(count).fetchone()[0] == before
    assert digest(a.store) == digest(b.store)
