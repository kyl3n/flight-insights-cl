"""The first fault of a flight's incident, and the incident timeline's lanes: on flights
built in memory, and on the SITL failsafe flights imported with their tlogs."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import flight, table

from flight_insights import views
from flight_insights.analyzers import ANALYZERS, FlightData, Registry
from flight_insights.analyzers.incident import INCIDENT, Event, first_fault
from flight_insights.analyzers.model import Result
from flight_insights.knowledge import Pack, load_pack

ARM, DISARM = 10_000_000, 120_000_000
S = 1_000_000
ERR = {"RADIO": 2, "FAILSAFE_RADIO": 5, "FAILSAFE_BATT": 6, "FAILSAFE_GCS": 8,
       "CRASH_CHECK": 12, "THRUST_LOSS_CHECK": 25}  # fmt: skip
REASON = {"RC_COMMAND": 1, "BATTERY_FAILSAFE": 4, "GCS_FAILSAFE": 5}
LOITER, RTL, LAND = 5, 6, 9
SESSION = {"sysid": 1, "compid": 1, "source_file_id": "s"}


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


def err(rows: Sequence[tuple[int, str, int]]) -> pa.Table:
    return table([r[0] for r in rows], {"Subsys": (pa.uint8(), [ERR[r[1]] for r in rows]),
                                        "ECode": (pa.uint8(), [r[2] for r in rows])})  # fmt: skip


def mode(rows: Sequence[tuple[int, int, str]]) -> pa.Table:
    return table([r[0] for r in rows], {
        "Mode": (pa.uint8(), [r[1] for r in rows]), "ModeNum": (pa.uint8(), [r[1] for r in rows]),
        "Rsn": (pa.uint8(), [REASON[r[2]] for r in rows]),
    })  # fmt: skip


@dataclasses.dataclass
class Linked(FlightData):
    """A flight whose tlog packets are given directly, on the flight's clock."""

    packets: pa.Table | None = None

    def tlog_packets(self) -> list[tuple[Mapping[str, Any], pa.Table]]:
        return [(SESSION, self.packets)] if self.packets is not None else []


def linked(base: FlightData, times: Sequence[int]) -> Linked:
    n = len(times)
    packets = pa.Table.from_arrays(
        [pa.array([float(t) for t in times], pa.float64()), pa.array([1] * n, pa.uint8()),
         pa.array([1] * n, pa.uint8())],
        names=["TimeUS", "src_sysid", "src_compid"],
    )  # fmt: skip
    fields = {f.name: getattr(base, f.name) for f in dataclasses.fields(FlightData)}
    return Linked(**fields, packets=packets)


def run(reg: Registry, f: FlightData) -> Result:
    r = INCIDENT.run(f, reg)
    assert r.skipped is None
    return r


def found(r: Result) -> dict[str, dict[str, Any]]:
    return {f.code: dict(f.params) for f in r.findings}


# --- the first fault ---------------------------------------------------------------------


def test_a_link_outage_explains_the_gcs_failsafe_and_its_rtl(reg: Registry) -> None:
    heard = [t for t in range(ARM - 5 * S, DISARM, 200_000) if not 40 * S < t < 60 * S]
    base = flight({
        "ERR": err([(45 * S, "FAILSAFE_GCS", 1), (61 * S, "FAILSAFE_GCS", 0)]),
        "MODE": mode([(ARM - S, LOITER, "RC_COMMAND"), (45 * S, RTL, "GCS_FAILSAFE")]),
    }, ARM, DISARM)  # fmt: skip
    r = run(reg, linked(base, heard))
    assert found(r) == {"incident.first_fault": {
        "first": "a telemetry link outage", "first_at_s": 30.0,
        "main": "the GCS failsafe", "main_at_s": 35.0,
        "chain": "a telemetry link outage → the GCS failsafe", "explained": 1,
    }}  # fmt: skip
    [ev] = r.evidence
    assert ev.values["chain"] == ["link_outage", "error:FAILSAFE_GCS"]


def test_the_earliest_cause_starts_the_chain(reg: Registry) -> None:
    """A late RC frame, then the radio failsafe: the receiver's own flag came first."""
    t = [ARM + k * 20_000 for k in range(4000)]
    flags = [5 if 30 * S <= x < 33 * S else 1 for x in t]
    base = flight({
        "RCI2": table(t, {"Flags": (pa.uint8(), flags), "OMask": (pa.uint16(), [0] * len(t))}),
        "ERR": err([(31 * S, "RADIO", 2), (32 * S, "FAILSAFE_RADIO", 1)]),
    }, ARM, DISARM)  # fmt: skip
    params = found(run(reg, base))["incident.first_fault"]
    assert params["chain"] == "the receiver's own failsafe → a late RC frame → the radio failsafe"
    assert params["first_at_s"] == 20.0


def test_a_parameter_changed_in_flight_before_a_thrust_loss(reg: Registry) -> None:
    parm = table([0, 50 * S], {"Name": (pa.string(), ["MOT_SPIN_MIN"] * 2),
                               "Value": (pa.float32(), [0.15, 0.05])})  # fmt: skip
    errors = err([(70 * S, "THRUST_LOSS_CHECK", 1), (72 * S, "CRASH_CHECK", 1)])
    base = flight({"PARM": parm, "ERR": errors}, ARM, DISARM)
    params = found(run(reg, base))["incident.first_fault"]
    assert params["chain"] == "a parameter changed in flight → a thrust loss → the crash check"
    assert params["main"] == "the crash check"


def test_an_error_nothing_earlier_explains(reg: Registry) -> None:
    r = run(reg, flight({"ERR": err([(50 * S, "CRASH_CHECK", 1)])}, ARM, DISARM))
    assert found(r) == {"incident.unexplained": {
        "first": "the crash check", "first_at_s": 40.0, "main": "the crash check",
        "main_at_s": 40.0, "chain": "the crash check", "explained": 0,
    }}  # fmt: skip


def test_no_incident_without_an_error_in_flight(reg: Registry) -> None:
    before = err([(ARM - 2 * S, "FAILSAFE_GCS", 1), (ARM - S, "FAILSAFE_GCS", 0)])
    assert run(reg, flight({"ERR": before}, ARM, DISARM)).findings == []
    assert run(reg, flight({}, ARM, DISARM)).findings == []


def test_a_cause_outside_its_window_is_not_one() -> None:
    rules = [["battery_low", "error:FAILSAFE_BATT", 30]]
    low = Event(10 * S, "battery_low")
    fired = Event(50 * S, "error:FAILSAFE_BATT")
    incident = first_fault([low, fired], 0, rules)
    assert incident is not None and [e.kind for e in incident.chain] == ["error:FAILSAFE_BATT"]
    near = Event(25 * S, "battery_low")
    incident = first_fault([near, fired], 0, rules)
    assert incident is not None and incident.chain[0] is near


def test_registered_with_its_findings(reg: Registry) -> None:
    assert INCIDENT in ANALYZERS
    assert all(code in reg.findings for code in INCIDENT.findings)


# --- the SITL failsafe flights -----------------------------------------------------------


@pytest.fixture(scope="module")
def sitl(tmp_path_factory: pytest.TempPathFactory, pack: Pack) -> dict[str, FlightData]:
    from test_imports import airframe

    from flight_insights.imports.commit import Decision, commit
    from flight_insights.imports.propose import propose
    from flight_insights.imports.scan import scan
    from flight_insights.store.cache import ColumnCache
    from flight_insights.store.db import open_store
    from flight_insights.store.keys import StoreKeys

    fixtures = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
    keys = StoreKeys.generate()
    folder = tmp_path_factory.mktemp("incident")
    out: dict[str, FlightData] = {}
    store = open_store(folder / "i.db", keys, "e" * 32)
    cache = ColumnCache(folder / "cache", keys)
    for name in ("copter-quad-fs-gcs", "copter-quad-fs-battery", "copter-quad-fs-radio"):
        batch = scan(store, pack, bytes(32), [fixtures / f"{name}.bin", fixtures / f"{name}.tlog"])
        af = airframe(store, pack, name)
        decisions = [Decision(i.id, airframe_id=af if i.kind == "dataflash" else None)
                     for i in batch]  # fmt: skip
        onboard, _ = commit(store, pack, cache, batch, propose(store, pack, batch), decisions)
        out[name] = FlightData.load(store, cache, pack, onboard.flights[0])
    return out


@pytest.mark.parametrize(
    ("name", "chain", "at_s"),
    [
        ("copter-quad-fs-gcs", ["gcs_unheard", "error:FAILSAFE_GCS"], [9.9, 14.9]),
        ("copter-quad-fs-battery", ["battery_low", "error:FAILSAFE_BATT"], [12.2, 14.3]),
        ("copter-quad-fs-radio", ["receiver_throttle", "error:FAILSAFE_RADIO"], [10.2, 10.2]),
    ],
)
def test_sitl_the_first_fault_of_each_failsafe(
    reg: Registry, sitl: dict[str, FlightData], name: str, chain: list[str], at_s: list[float]
) -> None:
    r = run(reg, sitl[name])
    [f] = r.findings
    assert f.code == "incident.first_fault"
    assert f.evidence[0].values["chain"] == chain
    assert f.evidence[0].values["at_s"] == pytest.approx(at_s, abs=0.05)


def test_sitl_the_timeline_puts_every_lane_on_one_clock(
    pack: Pack, sitl: dict[str, FlightData]
) -> None:
    view = views.timeline(sitl["copter-quad-fs-gcs"], pack)
    lanes = view["lanes"]
    assert {"link", "errors", "mode", "commands", "warnings", "gnss_fix"} <= set(lanes)
    first = view["first_fault"]
    assert [e["kind"] for e in first["chain"]] == ["gcs_unheard", "error:FAILSAFE_GCS"]
    assert first["chain"][0]["words"] == "the ground station unheard"
    # The warning's text stays on this laptop, and is shown here.
    assert any(w["text"] == "GCS Failsafe" for w in lanes["warnings"])
    rtl = [m for m in lanes["mode"] if m["mode"] == "RTL"]
    assert rtl and rtl[0]["reason"] == "GCS_FAILSAFE"
    assert rtl[0]["at_s"] == pytest.approx(first["main"]["at_s"], abs=0.05)
    assert all(s["value"] >= 3 for s in lanes["gnss_fix"][1:])  # SITL keeps its 3D fix
    assert np.isfinite(view["armed_s"])
