"""The procedure rehearsal without a simulator: it refuses anything but SITL on this
computer, carries out a procedure's steps in order, and its rehearsals are sound.

Run with the engine's environment:
  uv run --project engine pytest tools/procedures/tests
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rehearse
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import load_pack
from flight_insights.params.files import write_mission_planner
from flight_insights.platforms.schema import load_schema
from flight_insights.recommend.procedure import ProcedureStep
from rehearsals import REHEARSALS
from run_scenario import ScenarioFailed

LOOPBACK = ("127.0.0.1", 5760)


class Socket:
    def __init__(self, peer: str) -> None:
        self.peer = peer

    def getpeername(self) -> tuple[str, int]:
        return (self.peer, 5760)


class Conn:
    def __init__(self, dest: str = "127.0.0.1", peer: str = "127.0.0.1") -> None:
        self.destination_addr = (dest, 5760)
        self.port = Socket(peer)


class Link:
    """A vehicle link: `sim` says whether the vehicle has the simulator's parameters."""

    def __init__(self, conn: Conn, sim: bool = True) -> None:
        self.conn = conn
        self.sim = sim
        self.written: list[str] = []

    def get_param(self, name: str) -> float:
        if not self.sim:
            raise ScenarioFailed(f"no value for {name}")
        return 10.0

    def set_param(self, name: str, value: float) -> None:
        self.written.append(name)


# --- refusing anything but SITL on this computer --------------------------------------------


@pytest.mark.parametrize(
    ("host", "ok"),
    [("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("localhost", False),
     ("10.0.0.5", False), ("192.168.1.10", False), ("", False)],
)  # fmt: skip
def test_only_a_literal_loopback_address_is_this_computer(host: str, ok: bool) -> None:
    assert rehearse._loopback(host) is ok


def test_a_simulator_on_this_computer_is_accepted() -> None:
    rehearse.check_simulator(Link(Conn()), LOOPBACK)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("configured", "conn"),
    [(("10.0.0.5", 5760), Conn()), (LOOPBACK, Conn(dest="10.0.0.5")),
     (LOOPBACK, Conn(peer="192.168.1.10")), (("localhost", 5760), Conn())],
)  # fmt: skip
def test_any_address_off_this_computer_is_refused(configured: tuple[str, int], conn: Conn) -> None:
    with pytest.raises(rehearse.NotASimulator, match="not this computer"):
        rehearse.check_simulator(Link(conn), configured)  # type: ignore[arg-type]


def test_a_vehicle_without_the_simulators_parameters_is_refused() -> None:
    with pytest.raises(rehearse.NotASimulator, match="not a simulator"):
        rehearse.check_simulator(Link(Conn(), sim=False), LOOPBACK)  # type: ignore[arg-type]


def test_nothing_is_written_over_a_link_that_failed_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_aircraft = Link(Conn(), sim=False)

    class Vehicle:
        def __new__(cls, _tlog: Path) -> Link:  # type: ignore[misc]
            return real_aircraft

    monkeypatch.setattr(rehearse, "Vehicle", Vehicle)
    monkeypatch.setattr(Link, "wait_ready", lambda _self: None, raising=False)
    sim = object.__new__(rehearse.Simulator)
    sim.work, sim.links, sim._vehicle = tmp_path, 0, None
    with pytest.raises(rehearse.NotASimulator):
        sim.connect()
    with pytest.raises(rehearse.NotASimulator):
        sim.set("INS_HNTCH_ENABLE", 1)
    assert real_aircraft.written == []


# --- carrying out the steps -----------------------------------------------------------------


class FakeSim:
    """A vehicle whose notch settings appear once the notch is switched on."""

    GATED = ("INS_HNTCH_MODE", "INS_HNTCH_FREQ")

    def __init__(self) -> None:
        self.values: dict[str, float] = {
            "INS_HNTCH_ENABLE": 0, "INS_HNTCH_MODE": 1, "INS_HNTCH_FREQ": 80,
            "LOG_BITMASK": 180222,
        }  # fmt: skip
        self.reboots = 0

    def params(self) -> dict[str, float]:
        out = dict(self.values)
        if not out["INS_HNTCH_ENABLE"]:
            for name in self.GATED:
                out.pop(name, None)
        return out

    def set(self, name: str, value: float) -> None:
        if name in self.GATED and not self.values["INS_HNTCH_ENABLE"]:
            raise AssertionError(f"{name} is not there to set")
        self.values[name] = value

    def reboot(self) -> None:
        self.reboots += 1


def _step(kind: str, step: int | None = None, params: tuple[str, ...] = ()) -> ProcedureStep:
    return ProcedureStep(kind, {"en": kind, "uk": kind}, params, step)  # type: ignore[arg-type]


def _files(tmp_path: Path) -> dict[str, Path]:
    files = {
        "step 1": {"INS_HNTCH_ENABLE": 1.0},
        "step 2": {"INS_HNTCH_MODE": 3.0, "INS_HNTCH_FREQ": 85.0, "LOG_BITMASK": 2801662.0},
    }
    out: dict[str, Path] = {}
    for name, values in files.items():
        out[name] = tmp_path / f"{name.replace(' ', '')}.param"
        out[name].write_text(write_mission_planner(values, {}, []), encoding="utf-8")
    return out


def test_a_procedure_with_its_refresh_loads_every_file(tmp_path: Path) -> None:
    sim = FakeSim()
    report = rehearse.Report("t")
    steps = [
        _step("set", 1), _step("refresh"), _step("set", 2), _step("reboot"),
        _step("temporary", params=("LOG_BITMASK",)), _step("fly"),
    ]  # fmt: skip
    loaded = rehearse.carry_out(sim, steps, _files(tmp_path), set(sim.params()), report)  # type: ignore[arg-type]
    assert report.passed, [c for c in report.checks if not c.passed]
    assert loaded["INS_HNTCH_MODE"] == 3.0 and sim.reboots == 1
    assert sim.values["INS_HNTCH_FREQ"] == 85.0


def test_a_missing_refresh_is_caught(tmp_path: Path) -> None:
    sim = FakeSim()
    report = rehearse.Report("t")
    steps = [_step("set", 1), _step("set", 2)]
    rehearse.carry_out(sim, steps, _files(tmp_path), set(sim.params()), report)  # type: ignore[arg-type]
    [failed] = [c for c in report.checks if not c.passed]
    assert failed.name == "step 2: every parameter is listed when loaded"
    assert failed.detail == ["INS_HNTCH_FREQ", "INS_HNTCH_MODE"]


def test_a_test_flight_setting_missing_from_the_files_is_caught(tmp_path: Path) -> None:
    sim = FakeSim()
    report = rehearse.Report("t")
    steps = [_step("set", 1), _step("temporary", params=("INS_RAW_LOG_OPT",))]
    rehearse.carry_out(sim, steps, _files(tmp_path), set(sim.params()), report)  # type: ignore[arg-type]
    assert [c.detail for c in report.checks if not c.passed] == [["INS_RAW_LOG_OPT"]]


def test_values_compare_as_the_autopilot_stores_them() -> None:
    assert rehearse.same(0.1, 0.10000000149)  # the same float32
    assert not rehearse.same(0.1, 0.1001)
    assert rehearse.same(None, None) and not rehearse.same(None, 0.0)


# --- the rehearsals -------------------------------------------------------------------------


def test_every_rehearsal_plants_known_findings_and_answers_real_questions() -> None:
    pack = load_pack()
    reg = Registry.load(pack)
    fields = set(load_schema(pack).fields)
    assert REHEARSALS
    for r in REHEARSALS.values():
        assert r.setup.binary == "arducopter"
        assert r.expects and all(code in reg.findings for code in r.expects), r.name
        assert set(r.answers) <= fields, r.name


def test_a_rehearsal_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Sim:
        def __init__(self, *_a: Any) -> None:
            pass

        def connect(self) -> None:
            raise rehearse.NotASimulator("refusing a link to 10.0.0.5: not this computer")

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(rehearse, "Simulator", Sim)
    monkeypatch.setattr(rehearse, "sitl_command", lambda *_a: [])
    r = REHEARSALS["quad-failsafes"]
    report = rehearse.rehearse(r, tmp_path, tmp_path / "out", tmp_path)
    assert not report.passed and report.error and "not this computer" in report.error
