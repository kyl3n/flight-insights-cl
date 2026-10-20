"""The knowledge pack and parameter dialects."""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.formats import text
from flight_insights.knowledge import Pack, load_pack
from flight_insights.params.dialect import Dialect

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


def test_class_rules_are_well_formed(pack: Pack) -> None:
    rules = pack.json("params/classes.json")["rules"]
    allowed = {"tuning", "calibration", "learned", "counter", "hardware", "ground station"}
    assert {r["class"] for r in rules} <= allowed
    assert rules[-1]["class"] == "tuning" and rules[-1]["names"] == ["*"]
    for r in rules:
        assert r.get("why"), r
        for pattern in r.get("names", []):
            assert fnmatch.translate(pattern)
