"""The analysis framework: registry, flight data, runs, and the vibration analyzer."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import FakeCache

from flight_insights.analyzers import (
    ANALYZERS,
    Analyzer,
    AnalyzerBug,
    Evidence,
    Finding,
    FlightData,
    FlightError,
    Registry,
    Result,
    analysis_results,
    run_analysis,
)
from flight_insights.analyzers.vibration import VIBRATION
from flight_insights.imports import Decision, commit, propose, scan
from flight_insights.knowledge import KnowledgeError, Pack, load_pack
from flight_insights.store import Store, StoreKeys, open_store
from flight_insights.store.cache import ColumnCache

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl" / "4.7.0"
QUAD = FIXTURES / "copter-quad-hover.bin"
LAPTOP = "e" * 32
KEY = bytes(range(32))


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


def imported(store: Store, pack: Pack, cache: ColumnCache, log: Path = QUAD) -> str:
    """Import a fixture log into the hold queue and return its flight's id."""
    batch = scan(store, pack, KEY, [log])
    [result] = commit(store, pack, cache, batch, propose(store, pack, batch), [Decision("f1")])
    [flight] = result.flights
    return flight


# --- the registry ---------------------------------------------------------------------


def test_nothing_an_analyzer_emits_goes_unexplained(pack: Pack) -> None:
    reg = Registry.load(pack)
    for a in ANALYZERS:
        for m in a.metrics:
            assert m in reg.metrics, (a.name, m)
        for f in a.findings:
            if f.endswith(".*"):  # a family from the pack, such as the Better options
                assert any(code.startswith(f[:-1]) for code in reg.findings), (a.name, f)
            else:
                assert f in reg.findings, (a.name, f)
    for m in reg.metrics.values():  # every field the explanation card needs
        assert m.name["uk"] and m.definition["uk"] and m.method["uk"] and m.source["title"]


def test_zones_have_closed_low_and_open_high_ends(pack: Pack) -> None:
    vib = Registry.load(pack).metrics["vibration.p95"]
    assert [vib.zone(v) for v in (0.0, 29.9, 30.0, 59.9, 60.0, 500.0)] == [
        "target", "target", "caution", "caution", "out", "out",
    ]  # fmt: skip
    assert vib.zone(float("nan")) is None


def copy_pack(pack: Pack, tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    shutil.copytree(pack.root, root, ignore=shutil.ignore_patterns("params", "tools"))
    return root


BROKEN = {  # a registry file edited badly, and what the engine says about it
    "a name without Ukrainian": (
        ["metrics", "vibration.p95", "name"],
        "pop",
        "uk",
        "name needs text",
    ),
    "no zones": (["metrics", "vibration.p95"], "pop", "zones", "needs zones"),
    "an unknown source kind": (
        ["metrics", "vibration.p95", "source"],
        "set",
        ("kind", "hearsay"),
        "source",
    ),
    "an unknown stage": (["findings", "vibration.high"], "set", ("stage", "later"), "stage"),
    "a placeholder in one language only": (
        ["findings", "vibration.high", "title"],
        "set",
        ("uk", "Вібрація {level}"),
        "same placeholders",
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_an_incomplete_registry_entry_is_refused(pack: Pack, tmp_path: Path, case: str) -> None:
    where, action, what, message = BROKEN[case]
    root = copy_pack(pack, tmp_path)
    path = root / "metrics" / "vibration.json"
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    entry: dict[str, Any] = doc
    for key in where:
        entry = entry[key]
    if action == "pop":
        del entry[cast(str, what)]
    else:
        key, value = cast(tuple[str, str], what)
        entry[key] = value
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(KnowledgeError, match=message):
        Registry.load(load_pack(root))


def test_the_knowledge_version_follows_the_files(pack: Pack, tmp_path: Path) -> None:
    root = copy_pack(pack, tmp_path)
    before = Registry.load(load_pack(root)).version
    hover = root / "rules" / "hover.json"
    hover.write_text(hover.read_text(encoding="utf-8").replace("5.0", "6.0"), encoding="utf-8")
    assert Registry.load(load_pack(root)).version != before


# --- a flight as analyzers see it -------------------------------------------------------


def test_an_imported_flight_has_its_fingerprint_and_no_positions(
    store: Store, pack: Pack, cache: ColumnCache
) -> None:
    flight = FlightData.load(store, cache, pack, imported(store, pack, cache))
    assert flight.vehicle == "copter" and flight.params["INS_GYRO_FILTER"] > 0
    assert flight.fingerprint["board"]["id_hash"] is None  # the flight row holds the hash
    [hover] = flight.hover()
    assert flight.arm_us <= hover.start_us < hover.end_us <= flight.disarm_us
    gps = flight.table("GPS")
    assert gps is not None and gps.num_rows > 0
    assert {"Lat", "Lng"}.isdisjoint(gps.column_names)
    assert flight.table("NOSUCH") is None


def test_an_unknown_flight_cannot_be_analysed(store: Store, pack: Pack, cache: ColumnCache) -> None:
    with pytest.raises(FlightError):
        run_analysis(store, cache, pack, "0" * 32)


# --- runs -------------------------------------------------------------------------------


def test_a_run_is_stored_and_reused_while_current(
    store: Store, pack: Pack, cache: ColumnCache
) -> None:
    fid = imported(store, pack, cache)
    first = run_analysis(store, cache, pack, fid)
    assert first["current"] and first["status"] == "complete"
    metrics = {(m["metric"], m["segment"]): m for m in first["metrics"]}
    vib = metrics["vibration.p95", "hover"]
    assert vib["zone"] == "target" and 0 < vib["value"] < 30  # SITL flies smoothly
    assert metrics["vibration.clipping", "flight"]["value"] == 0
    codes = {f["code"] for f in first["findings"]}
    assert "logging.no_prefilter_gyro" in codes  # no GYR
    assert not [f for f in first["findings"] if f["analyzer"] == "vibration"]
    cited = {e["id"] for e in first["evidence"]}
    assert all(m["evidence"] in cited for m in first["metrics"])
    assert run_analysis(store, cache, pack, fid)["run"] == first["run"]  # nothing recomputed
    again = run_analysis(store, cache, pack, fid, force=True)
    assert again["run"] != first["run"] and again["metrics"] == first["metrics"]
    assert analysis_results(store, fid, Registry.load(pack))["run"] == again["run"]  # type: ignore[index]


def test_a_newer_analyzer_makes_the_stored_run_stale(
    store: Store, pack: Pack, cache: ColumnCache
) -> None:
    fid = imported(store, pack, cache)
    run_analysis(store, cache, pack, fid)
    newer = Analyzer("vibration", 2, VIBRATION.run, VIBRATION.metrics, VIBRATION.findings)
    reg = Registry.load(pack)
    assert not analysis_results(store, fid, reg, [newer])["current"]  # type: ignore[index]
    assert run_analysis(store, cache, pack, fid, [newer])["current"]


def test_one_failing_analyzer_leaves_the_others_standing(
    store: Store, pack: Pack, cache: ColumnCache
) -> None:
    def broken(_f: FlightData, _r: Registry) -> Result:
        raise ZeroDivisionError("oops")

    fid = imported(store, pack, cache)
    out = run_analysis(store, cache, pack, fid, [VIBRATION, Analyzer("broken", 1, broken, (), ())])
    assert out["status"] == "partial"
    assert out["analyzers"]["broken"]["skipped"].startswith("failed: ZeroDivisionError")
    assert any(m["metric"] == "vibration.p95" for m in out["metrics"])


def test_an_undeclared_metric_is_a_bug(store: Store, pack: Pack, cache: ColumnCache) -> None:
    def sneaky(_f: FlightData, _r: Registry) -> Result:
        return Result(findings=[Finding("vibration.high", "caution", "low", (EV,))])

    fid = imported(store, pack, cache)
    with pytest.raises(AnalyzerBug, match="does not declare"):
        run_analysis(store, cache, pack, fid, [Analyzer("sneaky", 1, sneaky, (), ())])


EV = Evidence("x", "fact", "something")


def test_a_finding_needs_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        Finding("vibration.high", "caution", "low", ())


# --- vibration ------------------------------------------------------------------------------


def vibe(levels: list[float], clip: list[int], step_us: int = 100_000) -> pa.Table:
    n = len(levels)
    meta = {b"unit": b"m/s/s", b"multiplier": b"1.0"}
    schema = pa.schema(
        [
            pa.field("TimeUS", pa.uint64(), metadata={b"unit": b"s", b"multiplier": b"1e-06"}),
            pa.field("IMU", pa.uint8()),
            pa.field("VibeX", pa.float32(), metadata=meta),
            pa.field("VibeY", pa.float32(), metadata=meta),
            pa.field("VibeZ", pa.float32(), metadata=meta),
            pa.field("Clip", pa.uint32()),
        ]
    )
    columns: list[pa.Array[Any]] = [
        pa.array([1_000_000 + i * step_us for i in range(n)], pa.uint64()),
        pa.array([0] * n, pa.uint8()),
        pa.array(levels, pa.float32()),
        pa.array([1.0] * n, pa.float32()),
        pa.array([2.0] * n, pa.float32()),
        pa.array(clip, pa.uint32()),
    ]
    return pa.Table.from_arrays(columns, schema=schema)


def fake_flight(table: pa.Table, hover: list[tuple[int, int]]) -> FlightData:
    t = np.asarray(table.column("TimeUS").to_numpy())
    return FlightData(
        id="f",
        key="fl1-x",
        airframe_id=None,
        firmware="4.7.0",
        arm_us=int(t[0]),
        disarm_us=int(t[-1]),
        params={},
        fingerprint={
            "vehicle": "copter",
            "hover_windows": [{"start_us": a, "end_us": b} for a, b in hover],
        },
        cache=FakeCache({"VIBE": table}),  # type: ignore[arg-type]
    )


def test_strong_vibration_in_hover_is_a_finding_with_its_evidence(pack: Pack) -> None:
    levels = [10.0] * 50 + [45.0] * 150  # the hover runs at about 45 m/s/s on X
    flight = fake_flight(vibe(levels, [0] * 200), [(6_000_000, 20_900_000)])
    r = VIBRATION.run(flight, Registry.load(pack))
    [metric, clipping] = r.metrics
    assert (metric.metric, metric.segment, metric.zone) == ("vibration.p95", "hover", "caution")
    assert metric.value == pytest.approx(45.0)
    assert clipping.value == 0 and clipping.zone == "target"
    [finding] = r.findings
    assert (finding.code, finding.severity, finding.confidence) == (
        "vibration.high",
        "caution",
        "high",
    )
    assert finding.params == {"value": 45.0, "axis": "X", "imu": 0, "segment": "hover"}
    [ev] = finding.evidence
    assert ev.messages == ("VIBE",) and ev.values["by_axis_and_imu"]["X0"] == pytest.approx(45.0)


def test_without_hover_the_whole_flight_is_judged_with_less_confidence(pack: Pack) -> None:
    flight = fake_flight(vibe([70.0] * 40, [0] * 40), [])
    [finding] = VIBRATION.run(flight, Registry.load(pack)).findings
    assert (finding.severity, finding.confidence, finding.params["segment"]) == (
        "out", "medium", "flight",
    )  # fmt: skip


def test_clipping_counts_only_what_rose_while_armed(pack: Pack) -> None:
    clip = [7] * 10 + list(range(7, 157)) + [156] * 40  # 7 from the bench, 149 in flight
    flight = fake_flight(vibe([5.0] * 200, clip), [])
    r = VIBRATION.run(flight, Registry.load(pack))
    clipping = next(m for m in r.metrics if m.metric == "vibration.clipping")
    assert clipping.value == 149 and clipping.zone == "out"
    assert any(f.code == "vibration.clipping" and f.params == {"count": 149} for f in r.findings)


def test_a_log_without_vibe_is_skipped_not_judged(pack: Pack) -> None:
    flight = fake_flight(vibe([1.0], [0]), [])
    flight.cache = FakeCache({})  # type: ignore[assignment]
    r = VIBRATION.run(flight, Registry.load(pack))
    assert r.skipped and not r.metrics and not r.findings
