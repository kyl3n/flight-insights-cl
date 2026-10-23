"""Parameter history: snapshots, set IDs, diffs, change origins, export, storage."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from df_builder import LogBuilder

from flight_insights.dataflash import read_dataflash
from flight_insights.history import (
    Candidate,
    LoadedFile,
    ParamChange,
    ParamDiff,
    Snapshot,
    SnapshotError,
    choose_snapshot,
    diff_snapshots,
    export_snapshot,
    from_log,
    from_param_file,
    from_tlog_download,
    label_changes,
    load_snapshot,
    param_set_id,
    save_change_sets,
    save_snapshot,
)
from flight_insights.history.snapshot import SET_ID_CLASSES, same_value
from flight_insights.knowledge import Pack, load_pack
from flight_insights.params import Dialect, parse_params, read_param_file
from flight_insights.store import StoreKeys, open_store
from flight_insights.tlog import ParamDownload, ParamWrite

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sitl"
LAPTOP = "b" * 32


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


def snap(values: dict[str, float], firmware: str = "4.7.2", **kw: object) -> Snapshot:
    return Snapshot(values, "copter", firmware, "log start", "primary", **kw)  # type: ignore[arg-type]


# --- snapshots from a log -----------------------------------------------------------


def synthetic_log(tmp_path: Path) -> Path:
    b = LogBuilder()
    b.fmt(0x90, "VER", "QBBBZ", "TimeUS,Maj,Min,Pat,FWS")
    b.fmt(0x91, "PARM", "QNff", "TimeUS,Name,Value,Default")
    b.fmt(0x92, "ARM", "QB", "TimeUS,ArmState")
    b.write("VER", 50, 4, 7, 2, b"ArduCopter V4.7.2 (fb773567)")
    b.write("PARM", 100, b"ATC_RAT_RLL_P", 0.135, 0.135)
    b.write("PARM", 110, b"INS_HNTCH_BW", 40.0, 40.0)
    b.write("PARM", 200, b"ATC_RAT_RLL_P", 0.15, 0.135)  # changed before arming
    b.write("PARM", 250, b"ATC_RAT_RLL_P", 0.15, 0.135)  # repeated, not a change
    b.write("ARM", 300, 1)
    b.write("PARM", 400, b"INS_HNTCH_BW", 20.0, 40.0)  # changed in flight
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b.bytes())
    return path


def test_the_snapshot_is_the_set_at_first_arming(tmp_path: Path) -> None:
    with read_dataflash(synthetic_log(tmp_path)) as df:
        s = from_log(df)
        early = from_log(df, at_us=150)
    assert (s.vehicle, s.firmware, s.origin, s.confidence) == (
        "copter",
        "4.7.2",
        "log start",
        "primary",
    )
    assert s.time_us == 300
    assert s.values == {"ATC_RAT_RLL_P": pytest.approx(0.15), "INS_HNTCH_BW": 40.0}
    assert [(c.time_us, c.name, c.new) for c in s.changes] == [(400, "INS_HNTCH_BW", 20.0)]
    assert s.final_values()["INS_HNTCH_BW"] == 20.0
    assert early.values["ATC_RAT_RLL_P"] == pytest.approx(0.135)
    assert [c.time_us for c in early.changes] == [200, 400]


def test_a_parameter_first_seen_in_flight_does_not_move_the_snapshot(tmp_path: Path) -> None:
    """A script or ground station creates a parameter after arming: it is kept, but the
    snapshot stays at arming and the in-flight change before it stays a change."""
    b = LogBuilder()
    b.fmt(0x90, "VER", "QBBBZ", "TimeUS,Maj,Min,Pat,FWS")
    b.fmt(0x91, "PARM", "QNff", "TimeUS,Name,Value,Default")
    b.fmt(0x92, "ARM", "QB", "TimeUS,ArmState")
    b.write("VER", 50, 4, 7, 2, b"ArduCopter V4.7.2 (fb773567)")
    b.write("PARM", 100, b"INS_HNTCH_BW", 40.0, 40.0)
    b.write("ARM", 3_000_000, 1)
    b.write("PARM", 4_000_000, b"INS_HNTCH_BW", 20.0, 40.0)  # changed in flight
    b.write("PARM", 9_000_000, b"SCR_USER1", 7.0, 0.0)  # created by a script
    path = tmp_path / "late.bin"
    path.write_bytes(b.bytes())
    with read_dataflash(path) as df:
        s = from_log(df)
    assert s.time_us == 3_000_000
    assert s.values == {"INS_HNTCH_BW": 40.0, "SCR_USER1": 7.0}
    assert [(c.time_us, c.name, c.new) for c in s.changes] == [(4_000_000, "INS_HNTCH_BW", 20.0)]


@pytest.mark.parametrize(
    "log", sorted(FIXTURES.rglob("*.bin")), ids=lambda p: str(p.relative_to(FIXTURES))
)
def test_a_log_snapshot_matches_the_set_downloaded_before_the_flight(pack: Pack, log: Path) -> None:
    """Each simulator session downloaded every parameter just before flying. The log's
    snapshot agrees with it on every configuration value; only what changes on its own
    (counters, learned values, upload counts) may differ."""
    with read_dataflash(log) as df:
        s = from_log(df)
    assert s.firmware == log.parent.name
    saved = {e.name: e.value for e in read_param_file(log.with_suffix(".param")).entries}
    assert set(s.values) == set(saved)
    dialect = s.dialect(pack)
    for name, value in saved.items():
        if name.startswith("SIM_"):
            continue  # simulator settings; positions among them were shifted per file
        if dialect.param_class(name, saved) in SET_ID_CLASSES:
            # The committed files hold 7 significant digits (the fixture tool now
            # writes enough for float32), so compare to that precision.
            assert math.isclose(s.values[name], value, rel_tol=1e-6), name


def test_in_flight_changes_on_a_fixture_are_counters_and_learned_values(pack: Pack) -> None:
    with read_dataflash(FIXTURES / "4.7.0" / "copter-quad-hover.bin") as df:
        s = from_log(df)
    dialect = s.dialect(pack)
    classes = {dialect.param_class(c.name, s.values) for c in s.changes}
    assert {c.name for c in s.changes} >= {"STAT_FLTTIME", "MOT_THST_HOVER"}
    assert classes <= {"counter", "learned"}


def test_a_log_without_firmware_or_parameters_is_refused(tmp_path: Path) -> None:
    b = LogBuilder()
    b.fmt(0x91, "PARM", "QNff", "TimeUS,Name,Value,Default")
    b.write("PARM", 100, b"ATC_RAT_RLL_P", 0.1, 0.1)
    path = tmp_path / "nover.bin"
    path.write_bytes(b.bytes())
    with read_dataflash(path) as df, pytest.raises(SnapshotError, match="VER"):
        from_log(df)


# --- fallback sources ---------------------------------------------------------------


def test_fallback_snapshots_and_their_order() -> None:
    download = from_tlog_download(ParamDownload((1, 1), 0, 10, {"A_P": 1.0}), "copter", "4.7.2")
    qgc = parse_params("1\t1\tA_P\t2\t9\n1\t2\tCAN_X\t5\t9\n", "qgroundcontrol")
    file = from_param_file(qgc, "copter", "4.7.2")
    assert file.values == {"A_P": 2.0}  # another component's parameters are not the autopilot's
    assert (download.origin, download.confidence) == ("tlog download", "fallback")
    log = snap({"A_P": 3.0})
    downloads = [Candidate(1000, download)]
    files = [Candidate(500, file)]
    assert choose_snapshot(log, downloads, files, 2000) is log
    assert choose_snapshot(None, downloads, files, 2000) is download
    assert choose_snapshot(None, downloads, files, 800) is file  # download came later
    assert choose_snapshot(None, downloads, files, 100) is None


# --- parameter-set IDs --------------------------------------------------------------


def test_set_ids_follow_configuration_not_counters(pack: Pack) -> None:
    base = {"ATC_RAT_RLL_P": 0.135, "STAT_BOOTCNT": 5.0, "MAV1_EXTRA1": 4.0}
    base |= {"MOT_HOVER_LEARN": 2.0, "MOT_THST_HOVER": 0.35}
    sid = param_set_id(snap(base), pack)
    assert sid.startswith("ps1-")
    for noise in ({"STAT_BOOTCNT": 6.0}, {"MAV1_EXTRA1": 10.0}, {"MOT_THST_HOVER": 0.36}):
        assert param_set_id(snap(base | noise), pack) == sid, noise
    assert param_set_id(snap(base | {"ATC_RAT_RLL_P": 0.14}), pack) != sid
    # hover throttle is configuration once learning is off
    fixed = base | {"MOT_HOVER_LEARN": 0.0}
    assert param_set_id(snap(fixed | {"MOT_THST_HOVER": 0.36}), pack) != param_set_id(
        snap(fixed), pack
    )


def test_set_ids_survive_a_firmware_rename(pack: Pack) -> None:
    old = snap({"PSC_JERK_D": 5.0, "ATC_RAT_RLL_P": 0.135}, "4.7.0")
    new = snap({"PSC_D_JERK": 5.0, "ATC_RAT_RLL_P": 0.135}, "4.7.2")
    assert param_set_id(old, pack) == param_set_id(new, pack)


def test_set_ids_tell_the_fixture_sessions_apart(pack: Pack) -> None:
    ids: dict[str, str] = {}
    for name in ("copter-quad-hover", "copter-quad-rawimu"):
        with read_dataflash(FIXTURES / "4.7.0" / f"{name}.bin") as df:
            ids[name] = param_set_id(from_log(df), pack)
    assert ids["copter-quad-hover"] != ids["copter-quad-rawimu"]  # INS_RAW_LOG_OPT differs


# --- diffs and origins --------------------------------------------------------------


def test_a_firmware_upgrade_diff_hides_renames_and_shows_lost_values(pack: Pack) -> None:
    old = snap({"PSC_JERK_D": 5.0, "ATC_RAT_RLL_P": 0.135, "STAT_BOOTCNT": 3.0}, "4.7.0")
    new = snap({"PSC_D_JERK": 4.0, "ATC_RAT_RLL_P": 0.135, "STAT_BOOTCNT": 4.0}, "4.7.2")
    diffs = {d.name: d for d in diff_snapshots(old, new, pack)}
    assert set(diffs) == {"PSC_D_JERK", "STAT_BOOTCNT"}
    assert (diffs["PSC_D_JERK"].before, diffs["PSC_D_JERK"].after) == (5.0, 4.0)
    assert diffs["PSC_D_JERK"].param_class == "tuning"
    assert diffs["STAT_BOOTCNT"].param_class == "counter"
    assert diff_snapshots(new, new, pack) == []


def test_added_and_removed_parameters(pack: Pack) -> None:
    diffs = diff_snapshots(snap({"A_P": 1.0}), snap({"B_P": 2.0}), pack)
    assert [(d.name, d.kind) for d in diffs] == [("A_P", "removed"), ("B_P", "added")]


def write(name: str, value: float, result: str, reply: float | None = None) -> ParamWrite:
    return ParamWrite(5_000, (255, 190), (1, 1), name, value, result, reply, 5_100)  # type: ignore[arg-type]


def test_changes_are_labelled_with_their_origin_and_evidence(pack: Pack) -> None:
    dialect = Dialect(pack, "copter", "4.7.2")
    diffs = [
        ParamDiff("ATC_RAT_PIT_P", 0.1, 0.12, "tuning"),
        ParamDiff("ATC_RAT_RLL_P", 0.1, 0.12, "tuning"),
        ParamDiff("ATC_RAT_YAW_P", 0.2, 0.25, "tuning"),
        ParamDiff("INS_HNTCH_BW", 40.0, 20.0, "tuning"),
        ParamDiff("MOT_THST_HOVER", 0.35, 0.36, "learned"),
        ParamDiff("OLD_GONE", 1.0, None, "tuning"),
        ParamDiff("PSC_D_JERK", 5.0, 4.0, "tuning"),
    ]
    in_flight = [ParamChange(9_000, "MOT_THST_HOVER", 0.35, 0.36)]
    writes = [
        write("ATC_RAT_PIT_P", 0.12, "applied", 0.12),
        write("ATC_RAT_RLL_P", 0.12, "applied", 0.12),
        write("ATC_RAT_YAW_P", 0.25, "no reply"),  # never confirmed
        write("INS_HNTCH_BW", 25.0, "different", 20.0),  # the vehicle kept 20
    ]
    loaded = LoadedFile("file-1", parse_params("ATC_RAT_RLL_P,0.12\n"))
    labelled = {
        c.diff.name: c
        for c in label_changes(diffs, dialect, in_flight, writes, [loaded], tlog_source="tlog-1")
    }
    origins = {n: c.origin for n, c in labelled.items()}
    assert origins == {
        "ATC_RAT_PIT_P": "ground station edit",
        "ATC_RAT_RLL_P": "loaded file",
        "ATC_RAT_YAW_P": "unexplained",
        "INS_HNTCH_BW": "ground station edit",
        "MOT_THST_HOVER": "in flight",
        "OLD_GONE": "unexplained",
        "PSC_D_JERK": "unexplained",
    }
    for name, c in labelled.items():
        assert bool(c.evidence) == (c.origin != "unexplained"), name
    rll = {e.kind for e in labelled["ATC_RAT_RLL_P"].evidence}
    assert rll == {"param file", "tlog write"}
    assert labelled["ATC_RAT_PIT_P"].evidence[0].source == "tlog-1"


def test_origins_read_names_in_the_later_firmware_dialect(pack: Pack) -> None:
    dialect = Dialect(pack, "copter", "4.7.0")  # still on the old names
    diffs = [ParamDiff("PSC_D_JERK", 5.0, 4.0, "tuning")]
    loaded = LoadedFile("f", parse_params("PSC_JERK_D,4\n"))
    assert label_changes(diffs, dialect, files=[loaded])[0].origin == "loaded file"


# --- export -------------------------------------------------------------------------


def test_export_restores_configuration_in_the_target_dialect(pack: Pack) -> None:
    s = snap(
        {"PSC_JERK_D": 5.0, "ATC_RAT_RLL_P": 0.135, "STAT_BOOTCNT": 3.0, "INS_ACC_ID": 1234.0},
        "4.7.0",
    )
    mp = parse_params(export_snapshot(s, "mission planner", pack))
    assert {e.name: e.value for e in mp.entries} == {"PSC_JERK_D": 5.0, "ATC_RAT_RLL_P": 0.135}
    upgraded = parse_params(export_snapshot(s, "mission planner", pack, firmware="4.7.2"))
    assert {e.name for e in upgraded.entries} == {"PSC_D_JERK", "ATC_RAT_RLL_P"}
    everything = parse_params(export_snapshot(s, "qgroundcontrol", pack, include_all=True))
    assert everything.kind == "qgroundcontrol" and not everything.problems
    assert {e.name: e.value for e in everything.entries} == pytest.approx(s.values)


def test_a_fixture_snapshot_exports_and_reads_back(pack: Pack) -> None:
    with read_dataflash(FIXTURES / "4.7.2" / "copter-quad-hover.bin") as df:
        s = from_log(df)
    back = parse_params(export_snapshot(s, "mission planner", pack))
    assert not back.problems
    values = {e.name: e.value for e in back.entries}
    assert values and all(same_value(v, s.values[n]) for n, v in values.items())
    assert "STAT_BOOTCNT" not in values


# --- storage ------------------------------------------------------------------------


def test_snapshots_and_change_sets_are_stored(tmp_path: Path, pack: Pack) -> None:
    with read_dataflash(FIXTURES / "4.7.0" / "copter-quad-hover.bin") as df:
        s = from_log(df)
    with open_store(tmp_path / "fleet.db", StoreKeys.generate(), LAPTOP) as store:
        sid = save_snapshot(store, s, pack)
        back = load_snapshot(store, sid)
        assert back == s
        row = store.get("param_snapshot", sid)
        assert row is not None and row["set_id"] == param_set_id(s, pack)
        platform = store.insert("platform", {"name": "Quad", "vehicle": "copter"})
        airframe = store.insert("airframe", {"name": "Q1", "platform_id": platform})
        f1 = store.insert("flight", {"airframe_id": airframe, "param_snapshot_id": sid})
        f2 = store.insert("flight", {"airframe_id": airframe})
        later = snap(s.values | {"ATC_RAT_RLL_P": 0.2, "STAT_BOOTCNT": 99.0}, "4.7.0")
        labelled = label_changes(diff_snapshots(s, later, pack), s.dialect(pack))
        ids = save_change_sets(store, airframe, f1, f2, labelled)
        assert len(ids) == 1
        cs = store.get("change_set", ids[0])
        assert cs is not None and cs["origin"] == "unexplained"
        names = {c["name"]: c["class"] for c in json.loads(cs["details_json"])["changes"]}
        assert names == {"ATC_RAT_RLL_P": "tuning", "STAT_BOOTCNT": "counter"}


def test_nan_values_are_refused_not_lost(tmp_path: Path, pack: Pack) -> None:
    with open_store(tmp_path / "fleet.db", StoreKeys.generate(), LAPTOP) as store:
        with pytest.raises(SnapshotError, match="NaN"):
            save_snapshot(store, snap({"ATC_RAT_RLL_P": math.nan}), pack)
        assert store.conn.execute("SELECT count(*) FROM param_snapshot").fetchone() == (0,)
