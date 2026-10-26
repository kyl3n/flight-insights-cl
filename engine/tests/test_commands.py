"""Commands the aircraft received and what it answered, and how the operator answered
warnings, on flights built in memory: the onboard MAVC record, the tlog's own packets
from the ground station and the vehicle's replies, and STATUSTEXT severities."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights.analyzers import ANALYZERS, FlightData, Registry
from flight_insights.analyzers.commands import COMMANDS
from flight_insights.analyzers.model import Result
from flight_insights.knowledge import load_pack
from flight_insights.tlog.names import command_name

ARM, DISARM = 60_000_000, 200_000_000
S = 1_000_000
GCS, VEHICLE = 255, 1
SET_MODE, ARM_DISARM, TAKEOFF, REQUEST_MESSAGE = 176, 400, 22, 512
ACCEPTED, DENIED, UNSUPPORTED, FAILED = 0, 2, 3, 4
SESSION = {"sysid": VEHICLE, "compid": 1, "source_file_id": "s"}


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


def mavc(rows: Sequence[tuple[int, int, int, int]]) -> pa.Table:
    """(time, command, result, sender) rows of the onboard command record."""
    n = len(rows)
    return table([r[0] for r in rows], {
        "TS": (pa.uint8(), [VEHICLE] * n), "TC": (pa.uint8(), [1] * n),
        "SS": (pa.uint8(), [r[3] for r in rows]), "SC": (pa.uint8(), [190] * n),
        "Fr": (pa.uint8(), [0] * n), "Cmd": (pa.uint16(), [r[1] for r in rows]),
        "P1": (pa.float32(), [0.0] * n), "X": (pa.int32(), [0] * n),
        "Res": (pa.uint8(), [r[2] for r in rows]), "WL": (pa.uint8(), [1] * n),
    })  # fmt: skip


def mode(rows: Sequence[tuple[int, int, int]]) -> pa.Table:
    """(time, mode number, reason number)."""
    return table([r[0] for r in rows], {
        "Mode": (pa.uint8(), [r[1] for r in rows]), "ModeNum": (pa.uint8(), [r[1] for r in rows]),
        "Rsn": (pa.uint8(), [r[2] for r in rows]),
    })  # fmt: skip


@dataclasses.dataclass
class Recorded(FlightData):
    """A flight with tlog tables given directly, already on the flight's clock."""

    tlogs_given: dict[str, pa.Table] = dataclasses.field(default_factory=dict[str, pa.Table])

    def tlog(
        self, name: str, columns: list[str] | None = None
    ) -> list[tuple[Mapping[str, Any], pa.Table]]:
        found = self.tlogs_given.get(name)
        return [(SESSION, found)] if found is not None else []


def recorded(base: FlightData, **tlogs: pa.Table) -> Recorded:
    fields = {f.name: getattr(base, f.name) for f in dataclasses.fields(FlightData)}
    return Recorded(**fields, tlogs_given=dict(tlogs))


def packets(rows: Sequence[tuple[int, int]], sender: int, **columns: Sequence[int]) -> pa.Table:
    """(time, value) rows as the tlog keeps them: the sender's IDs, TimeUS on the flight's
    clock, and the named columns."""
    n = len(rows)
    arrays: dict[str, pa.Array[Any]] = {
        "TimeUS": pa.array([float(r[0]) for r in rows], pa.float64()),
        "src_sysid": pa.array([sender] * n, pa.uint8()),
        "src_compid": pa.array([190 if sender == GCS else 1] * n, pa.uint8()),
    }
    arrays |= {k: pa.array(list(v), pa.int64()) for k, v in columns.items()}
    return pa.Table.from_arrays(list(arrays.values()), names=list(arrays))


def run(reg: Registry, f: FlightData) -> Result:
    r = COMMANDS.run(f, reg)
    assert r.skipped is None
    return r


def found(r: Result) -> dict[str, dict[str, Any]]:
    return {f.code: dict(f.params) for f in r.findings}


def metric(r: Result, name: str) -> tuple[float, str | None]:
    [m] = [m for m in r.metrics if m.metric == name]
    return m.value, m.zone


# --- what the aircraft answered ----------------------------------------------------------


def test_a_command_refused_in_flight(reg: Registry) -> None:
    rows = [
        (ARM + 5 * S, SET_MODE, DENIED, GCS),
        (ARM + 6 * S, SET_MODE, ACCEPTED, GCS),
        (ARM + 7 * S, REQUEST_MESSAGE, UNSUPPORTED, GCS),  # a probe: counted apart
        (ARM + 9 * S, TAKEOFF, FAILED, VEHICLE),
    ]
    r = run(reg, flight({"MAVC": mavc(rows)}, ARM, DISARM))
    assert metric(r, "command.refused_in_flight") == (2.0, "caution")
    assert found(r) == {"command.refused_in_flight": {
        "count": 2, "command": "DO_SET_MODE", "result": "denied",
        "sender": "the ground station", "at_s": 5.0,
    }}  # fmt: skip
    [ev] = [e for e in r.evidence if e.id == "command:in_flight"]
    assert ev.values["not_supported"] == 1 and ev.values["commands"] == 4
    assert ev.fields == ("Cmd", "Res", "SS")  # never a command's parameters or coordinates


def test_a_flight_whose_commands_were_all_taken(reg: Registry) -> None:
    rows = [(ARM + k * S, SET_MODE, ACCEPTED, GCS) for k in range(1, 5)]
    r = run(reg, flight({"MAVC": mavc(rows)}, ARM, DISARM))
    assert metric(r, "command.refused_in_flight") == (0.0, "target")
    assert r.findings == []


@pytest.mark.parametrize(
    ("sender", "params", "who"),
    [
        (GCS, {}, "the ground station"),
        (250, {"MAV_GCS_SYSID": 250}, "the ground station"),
        (252, {"MAV_GCS_SYSID": 250, "MAV_GCS_SYSID_HI": 254}, "the ground station"),
        (VEHICLE, {}, "the aircraft's own components"),
        (42, {}, "another system"),
    ],
)
def test_who_sent_it(reg: Registry, sender: int, params: dict[str, float], who: str) -> None:
    f = flight({"MAVC": mavc([(ARM + S, SET_MODE, DENIED, sender)])}, ARM, DISARM, params)
    assert found(run(reg, f))["command.refused_in_flight"]["sender"] == who


def test_arming_refused_before_take_off(reg: Registry) -> None:
    rows = [
        (ARM - 30 * S, ARM_DISARM, FAILED, GCS),
        (ARM - 10 * S, ARM_DISARM, FAILED, GCS),
        (ARM - S // 2, ARM_DISARM, ACCEPTED, GCS),
    ]
    r = run(reg, flight({"MAVC": mavc(rows)}, ARM, DISARM))
    assert found(r) == {"command.arming_refused": {"count": 2, "waited_s": 30.0}}
    assert metric(r, "command.refused_in_flight") == (0.0, "target")  # not in flight


def test_command_names_come_from_mavlink() -> None:
    assert command_name(SET_MODE) == "DO_SET_MODE"
    assert command_name(ARM_DISARM) == "COMPONENT_ARM_DISARM"
    assert command_name(65000) == "command 65000"


# --- commands with no reply --------------------------------------------------------------


def test_commands_with_no_reply_split_by_whether_they_reached_the_aircraft(
    reg: Registry,
) -> None:
    sent = [
        (ARM + 10 * S, SET_MODE),  # answered
        (ARM + 20 * S, TAKEOFF),  # executed onboard, its reply lost
        (ARM + 30 * S, ARM_DISARM),  # sent twice, never arrived
        (ARM + 31 * S, ARM_DISARM),
    ]
    station = packets(sent, GCS, command=[c for _, c in sent], target_system=[VEHICLE] * 4)
    acks = packets([(ARM + 10 * S + 50_000, SET_MODE)], VEHICLE, command=[SET_MODE])
    onboard = mavc(
        [(ARM + 10 * S, SET_MODE, ACCEPTED, GCS), (ARM + 20 * S, TAKEOFF, ACCEPTED, GCS)]
    )
    f = recorded(flight({"MAVC": onboard}, ARM, DISARM), COMMAND_LONG=station, COMMAND_ACK=acks)
    r = run(reg, f)
    assert found(r)["command.unanswered"] == {
        "count": 2,
        "command": "NAV_TAKEOFF",
        "at_s": 20.0,
        "lost": 1,
        "never": 1,
    }
    [ev] = [e for e in r.evidence if e.id == "command:replies"]
    assert (ev.values["sent"], ev.values["unanswered"]) == (3, 2)  # the resend is one


def test_no_reply_check_without_the_stations_own_packets(reg: Registry) -> None:
    acks = packets([(ARM + S, SET_MODE)], VEHICLE, command=[SET_MODE])
    r = run(reg, recorded(flight({}, ARM, DISARM), COMMAND_ACK=acks))
    assert r.findings == [] and r.evidence == []


# --- warnings and the operator's answer --------------------------------------------------


def statustext(rows: Sequence[tuple[int, int]]) -> pa.Table:
    return packets(rows, VEHICLE, severity=[s for _, s in rows])


def test_a_warning_the_operator_answered_and_one_they_did_not(reg: Registry) -> None:
    warnings = statustext([
        (ARM + 10 * S, 4), (ARM + 11 * S, 4),  # one warning, repeated
        (ARM + 40 * S, 2),
        (ARM + 50 * S, 6),  # information: not a warning
        (ARM - 20 * S, 2),  # before arming
    ])  # fmt: skip
    onboard = mavc([(ARM + 15 * S, SET_MODE, ACCEPTED, GCS)])
    f = recorded(flight({"MAVC": onboard}, ARM, DISARM), STATUSTEXT=warnings)
    r = run(reg, f)
    assert found(r)["operator.unanswered_warning"] == {"count": 1, "within_s": "30", "at_s": 40.0}
    [ev] = [e for e in r.evidence if e.id == "operator:warnings"]
    assert (ev.values["warnings"], ev.values["answered"], ev.values["longest_s"]) == (2, 1, 5.0)


def test_a_pilots_mode_change_answers_a_warning(reg: Registry) -> None:
    warnings = statustext([(ARM + 10 * S, 4)])
    modes = mode([(ARM - S, 5, 1), (ARM + 22 * S, 6, 1)])  # LOITER, then RTL by the pilot
    f = recorded(flight({"MODE": modes}, ARM, DISARM), STATUSTEXT=warnings)
    r = run(reg, f)
    assert "operator.unanswered_warning" not in found(r)
    [ev] = [e for e in r.evidence if e.id == "operator:warnings"]
    assert ev.values["longest_s"] == 12.0


def test_a_failsafes_own_mode_change_is_not_the_operators(reg: Registry) -> None:
    warnings = statustext([(ARM + 10 * S, 4)])
    modes = mode([(ARM - S, 5, 1), (ARM + 10 * S, 6, 5)])  # RTL by the GCS failsafe
    f = recorded(flight({"MODE": modes}, ARM, DISARM), STATUSTEXT=warnings)
    assert found(run(reg, f))["operator.unanswered_warning"]["count"] == 1


def test_registered_with_its_metrics_and_findings(reg: Registry) -> None:
    assert COMMANDS in ANALYZERS
    assert all(m in reg.metrics for m in COMMANDS.metrics)
    assert all(code in reg.findings for code in COMMANDS.findings)


# --- the SITL failsafe flights -----------------------------------------------------------


@pytest.fixture(scope="module")
def sitl(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Result]:
    from pathlib import Path

    from test_imports import airframe

    from flight_insights.imports.commit import Decision, commit
    from flight_insights.imports.propose import propose
    from flight_insights.imports.scan import scan
    from flight_insights.store.cache import ColumnCache
    from flight_insights.store.db import open_store
    from flight_insights.store.keys import StoreKeys

    fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
    pack = load_pack()
    registry = Registry.load(pack)
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("commands")
    out: dict[str, Result] = {}
    with open_store(folder / "c.db", keys, "e" * 32) as store:
        cache = ColumnCache(folder / "cache", keys)
        for name in ("copter-quad-fs-gcs", "copter-quad-fs-battery"):
            batch = scan(
                store, pack, bytes(32), [fixtures / f"{name}.bin", fixtures / f"{name}.tlog"]
            )
            af = airframe(store, pack, name)
            decisions = [
                Decision(i.id, airframe_id=af if i.kind == "dataflash" else None) for i in batch
            ]
            onboard, _ = commit(store, pack, cache, batch, propose(store, pack, batch), decisions)
            out[name] = run(registry, FlightData.load(store, cache, pack, onboard.flights[0]))
    return out


def test_sitl_the_runners_commands_were_all_taken(sitl: dict[str, Result]) -> None:
    for r in sitl.values():
        assert metric(r, "command.refused_in_flight") == (0.0, "target")


@pytest.mark.parametrize(
    ("name", "count", "at_s"),
    [("copter-quad-fs-gcs", 1, 14.9), ("copter-quad-fs-battery", 2, 14.3)],
)
def test_sitl_failsafe_warnings_no_one_answered(
    sitl: dict[str, Result], name: str, count: int, at_s: float
) -> None:
    # The scripted flights fly on with no one at the controls once the failsafe warns.
    assert found(sitl[name])["operator.unanswered_warning"] == {
        "count": count, "within_s": "30", "at_s": at_s,
    }  # fmt: skip
