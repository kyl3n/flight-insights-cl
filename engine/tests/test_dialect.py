"""The knowledge pack and parameter dialects."""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.formats import text
from flight_insights.knowledge import Pack, load_pack
from flight_insights.params.dialect import Dialect
from flight_insights.params.validate import beyond_documented, metadata_problem

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
FIXTURE_LOGS = sorted(FIXTURES.rglob("*.bin"))


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


def vehicle_of(log: Path) -> str:
    return "plane" if "plane" in log.stem else "copter"


def logged_params(log: Path) -> dict[str, float]:
    with read_dataflash(log) as df:
        rows = df.table("PARM").to_pylist()
    return {text(r["Name"]): r["Value"] for r in rows}


def test_the_pack_covers_every_4_7_release(pack: Pack) -> None:
    assert pack.versions[:3] == ["4.7.0", "4.7.1", "4.7.2"]
    for version in pack.versions:
        assert len(pack.metadata(version, "copter")) > 5000
        assert len(pack.metadata(version, "plane")) > 5000


@pytest.mark.parametrize("log", FIXTURE_LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_every_parameter_in_the_fixtures_is_known_to_its_release(pack: Pack, log: Path) -> None:
    dialect = Dialect(pack, vehicle_of(log), log.parent.name)
    assert dialect.choice.exact
    params = logged_params(log)
    canonical = dialect.to_canonical(params)
    assert canonical.unknown == []
    assert len(canonical.values) == len(params)


def test_the_4_7_1_jerk_rename_reads_and_writes_both_ways(pack: Pack) -> None:
    old, new = Dialect(pack, "copter", "4.7.0"), Dialect(pack, "copter", "4.7.2")
    assert old.canonical("PSC_JERK_D", 5.0) == ("PSC_D_JERK", 5.0)
    assert new.canonical("PSC_D_JERK", 5.0) == ("PSC_D_JERK", 5.0)
    # a recommendation in canonical names reaches each vehicle in its own dialect
    assert old.from_canonical({"PSC_D_JERK": 4.0}) == {"PSC_JERK_D": 4.0}
    assert new.from_canonical({"PSC_D_JERK": 4.0}) == {"PSC_D_JERK": 4.0}
    plane = Dialect(pack, "plane", "4.7.0")
    assert plane.canonical("Q_P_JERK_NE")[0] == "Q_P_NE_JERK"


def test_names_and_scaling_round_trip_for_every_parameter(pack: Pack) -> None:
    for version in pack.versions:
        for vehicle in ("copter", "plane"):
            dialect = Dialect(pack, vehicle, version)
            values = {name: 1.5 for name in dialect.meta}
            assert dialect.from_canonical(dialect.to_canonical(values).values) == values


def test_unknown_parameters_are_flagged_not_dropped(pack: Pack) -> None:
    dialect = Dialect(pack, "copter", "4.7.2")
    result = dialect.to_canonical({"ATC_RAT_RLL_P": 0.135, "MY_SCRIPT_GAIN": 2.0})
    assert result.unknown == ["MY_SCRIPT_GAIN"]
    assert result.values == {"ATC_RAT_RLL_P": 0.135, "MY_SCRIPT_GAIN": 2.0}


@pytest.mark.parametrize(
    ("firmware", "used", "exact"),
    [("4.7.1", "4.7.1", True), ("4.7.9", "4.7.2", False), ("4.6.3", "4.7.0", False)],
)
def test_firmware_without_its_own_metadata_uses_the_nearest_earlier_release(
    pack: Pack, firmware: str, used: str, exact: bool
) -> None:
    choice = pack.choose(firmware)
    assert (choice.version, choice.exact) == (used, exact)


@pytest.mark.parametrize(
    ("name", "others", "expected"),
    [
        ("ATC_RAT_RLL_P", {}, "tuning"),
        ("INS_HNTCH_FREQ", {}, "tuning"),
        ("STAT_BOOTCNT", {}, "counter"),
        ("INS_ACCOFFS_X", {}, "calibration"),
        ("COMPASS_OFS_X", {}, "calibration"),
        ("COMPASS_OFS_X", {"COMPASS_LEARN": 3}, "learned"),
        ("MOT_THST_HOVER", {"MOT_HOVER_LEARN": 2}, "learned"),
        ("MOT_THST_HOVER", {"MOT_HOVER_LEARN": 0}, "tuning"),
        ("COMPASS_DEV_ID", {}, "hardware"),
        ("BARO1_GND_PRESS", {}, "learned"),
        ("MAV1_EXTRA1", {}, "ground station"),
        ("MAV1_OPTIONS", {}, "tuning"),
        ("H_COL_HOVER", {"H_HOVER_LEARN": 2}, "learned"),
        ("H_COL_HOVER", {"H_HOVER_LEARN": 0}, "tuning"),
        ("ARSPD_OFFSET", {"ARSPD_SKIP_CAL": 0}, "learned"),
        ("ARSPD2_OFFSET", {"ARSPD2_SKIP_CAL": 0}, "learned"),
        ("ARSPD_OFFSET", {"ARSPD_SKIP_CAL": 1}, "calibration"),
        ("MIS_TOTAL", {}, "ground station"),
    ],
)
def test_parameter_classes(pack: Pack, name: str, others: dict[str, float], expected: str) -> None:
    assert Dialect(pack, "copter", "4.7.2").param_class(name, others) == expected


def test_every_rename_carries_evidence(pack: Pack) -> None:
    renames = pack.json("params/renames.json")["renames"]
    assert renames
    for r in renames:
        assert r["evidence"]["kind"] in ("same storage", "conversion code"), r
        assert r["scale"] != 0
        old = pack.metadata(r["from_version"], r["vehicle"])
        new = pack.metadata(r["to_version"], r["vehicle"])
        assert r["old"] in old and r["old"] not in new, r
        assert r["new"] in new and r["new"] not in old, r


def test_supplement_entries_cite_their_source_and_are_still_needed(pack: Pack) -> None:
    for p in pack.json("params/supplement.json")["params"]:
        assert p["evidence"], p
        for vehicle in p["vehicles"]:
            for version in pack.versions:
                generated = pack.generated_metadata(version, vehicle)
                assert p["name"] not in generated, f"{p['name']} is now generated: remove it"
                assert pack.metadata(version, vehicle)[p["name"]]["supplement"] is True


def test_beyond_range_entries_cite_their_source_and_are_still_needed(pack: Pack) -> None:
    """A documented range the firmware does not enforce: each entry cites the source, and
    the documented range still stops short of what the firmware accepts."""
    for p in pack.json("params/beyond_range.json")["params"]:
        assert p["evidence"], p
        low, high = p["accepts"]
        for vehicle in p["vehicles"]:
            for version in pack.versions:
                meta = pack.metadata(version, vehicle)[p["name"]]
                assert meta["accepts"] == [low, high]
                documented = meta["range"]
                assert float(documented[1]) < high, f"{p['name']} is now documented: remove it"


def test_fs_dr_timeout_takes_what_the_firmware_takes(pack: Pack) -> None:
    for version in pack.versions:
        meta = pack.metadata(version, "copter")["FS_DR_TIMEOUT"]
        assert metadata_problem("FS_DR_TIMEOUT", 1800.0, meta, version) is None
        assert beyond_documented("FS_DR_TIMEOUT", 1800.0, meta) == (
            "beyond_documented",
            {"param": "FS_DR_TIMEOUT", "value": "1800", "low": "0", "high": "120"},
        )
        assert beyond_documented("FS_DR_TIMEOUT", 60.0, meta) is None  # documented
        problem = metadata_problem("FS_DR_TIMEOUT", 40000.0, meta, version)
        assert problem is not None and problem[0] == "out_of_range"  # past the 16-bit store
    other = pack.metadata(pack.versions[0], "copter")["FS_GCS_TIMEOUT"]
    assert "accepts" not in other


def test_class_rules_are_well_formed(pack: Pack) -> None:
    rules = pack.json("params/classes.json")["rules"]
    allowed = {"tuning", "calibration", "learned", "counter", "hardware", "ground station"}
    assert {r["class"] for r in rules} <= allowed
    assert rules[-1]["class"] == "tuning" and rules[-1]["names"] == ["*"]
    for r in rules:
        assert r.get("why"), r
        for pattern in r.get("names", []):
            assert fnmatch.translate(pattern)


# --- gates: what switches a parameter into view ---------------------------------------------


def test_every_release_has_a_gates_table_of_real_parameters(pack: Pack) -> None:
    for version in pack.versions:
        for vehicle in ("copter", "plane"):
            gates = pack.gates(version, vehicle)
            meta = pack.metadata(version, vehicle)
            assert len(gates) > 3000
            assert all(name in meta and g["gate"] in meta for name, g in gates.items())
            assert {g["after"] for g in gates.values()} == {"refresh", "reboot"}


@pytest.mark.parametrize(
    ("name", "gate", "after"),
    [
        ("INS_HNTCH_FREQ", "INS_HNTCH_ENABLE", "refresh"),  # AP_PARAM_FLAG_ENABLE
        ("INS_HNTC3_BW", "INS_HNTC3_ENABLE", "refresh"),
        ("FFT_MINHZ", "FFT_ENABLE", "refresh"),
        ("BATT2_LOW_VOLT", "BATT2_MONITOR", "refresh"),
        ("BATT2_AMP_PERVLT", "BATT2_MONITOR", "reboot"),  # the analog backend's own
        ("RNGFND1_MAX", "RNGFND1_TYPE", "refresh"),
        ("ARSPD_PIN", "ARSPD_TYPE", "refresh"),  # behind ARSPD_TYPE, itself behind ENABLE
        ("ARSPD_TYPE", "ARSPD_ENABLE", "refresh"),
    ],
)
def test_gates_are_read_from_the_source(pack: Pack, name: str, gate: str, after: str) -> None:
    assert Dialect(pack, "copter", "4.7.0").gate(name) == (gate, after)


def test_always_visible_parameters_have_no_gate(pack: Pack) -> None:
    dialect = Dialect(pack, "copter", "4.7.0")
    for name in ("INS_HNTCH_ENABLE", "ATC_RAT_RLL_P", "MOT_PWM_TYPE", "LOG_BITMASK"):
        assert dialect.gate(name) is None


@pytest.mark.parametrize("log", FIXTURE_LOGS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_no_logged_parameter_sits_behind_a_gate_that_is_off(pack: Pack, log: Path) -> None:
    # The firmware leaves hidden parameters out of the log (AP_Param::next_scalar), so a
    # logged parameter whose gate is 0 would mean the table is wrong.
    params = logged_params(log)
    gates = pack.gates(log.parent.name, vehicle_of(log))  # type: ignore[arg-type]
    wrong = [name for name in params if name in gates and params.get(gates[name]["gate"], 1) == 0]
    assert wrong == []
