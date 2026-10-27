"""The link analyzer on flights built in memory: downlink loss and outages from tlog
packets, uplink loss and ground-station silence from MAV, radio status from RAD or the
tlog's RADIO_STATUS (decibels only for SiK radios), and the RC link."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, flight, table

from flight_insights.analyzers import ANALYZERS, FlightData, Registry
from flight_insights.analyzers.links import LINKS
from flight_insights.analyzers.model import Result
from flight_insights.knowledge import load_pack

SESSION = {"sysid": 1, "compid": 1, "source_file_id": "s"}
SIK = (51, 68)


@pytest.fixture(scope="module")
def reg() -> Registry:
    return Registry.load(load_pack())


@dataclasses.dataclass
class Linked(FlightData):
    """A flight whose tlog sessions are given directly."""

    packets: list[pa.Table] = dataclasses.field(default_factory=list[pa.Table])
    radio: pa.Table | None = None

    def tlog_packets(self) -> list[tuple[Mapping[str, Any], pa.Table]]:
        return [(SESSION, p) for p in self.packets]

    def tlog(
        self, name: str, columns: list[str] | None = None
    ) -> list[tuple[Mapping[str, Any], pa.Table]]:
        if name == "RADIO_STATUS" and self.radio is not None:
            return [(SESSION, self.radio)]
        return []


def linked(
    base: FlightData, packets: Sequence[pa.Table] = (), radio: pa.Table | None = None
) -> Linked:
    fields = {f.name: getattr(base, f.name) for f in dataclasses.fields(FlightData)}
    return Linked(**fields, packets=list(packets), radio=radio)


def columns(**arrays: pa.Array[Any]) -> pa.Table:
    return pa.Table.from_arrays(list(arrays.values()), names=list(arrays))


def packets(
    times: Sequence[int], seqs: Sequence[int], sender: tuple[int, int] = (1, 1)
) -> pa.Table:
    n = len(times)
    return columns(
        tlog_time_us=pa.array(list(times), pa.int64()),
        tlog_record=pa.array(list(range(n)), pa.int64()),
        src_sysid=pa.array([sender[0]] * n, pa.int64()),
        src_compid=pa.array([sender[1]] * n, pa.int64()),
        src_seq=pa.array([s % 256 for s in seqs], pa.int64()),
        TimeUS=pa.array(list(times), pa.int64()),
    )  # fmt: skip


def codes(r: Result) -> dict[str, dict[str, Any]]:
    return {f.code: dict(f.params) for f in r.findings}


def metric(r: Result, name: str) -> tuple[float, str | None]:
    [m] = [m for m in r.metrics if m.metric == name]
    return m.value, m.zone


# --- downlink ---------------------------------------------------------------------------


def test_downlink_loss_and_outages_count_the_vehicles_packets_while_armed(reg: Registry) -> None:
    times = list(range(ARM, DISARM, 10_000))  # 100 Hz while armed
    seqs = list(range(len(times)))
    keep = [k for k in range(len(times)) if k % 20 != 7]  # one in 20 lost: 5%
    t = [times[k] for k in keep]
    gap_at = len(t) // 2
    t = [*t[:gap_at], *[x + 3_000_000 for x in t[gap_at:]]]  # a 3 s silence
    t = [x for x in t if x <= DISARM]
    s = [seqs[k] for k in keep][: len(t)]
    other = packets(t[:100], list(range(100)), sender=(255, 190))  # the station's own
    before = packets([ARM - 5_000_000 + k * 10_000 for k in range(100)], list(range(100)))
    pkts = pa.concat_tables([packets(t, s), other, before])
    f = linked(flight({}, ARM, DISARM), [pkts])
    r = LINKS.run(f, reg)
    assert metric(r, "link.downlink_loss") == (pytest.approx(5.0, abs=0.1), "caution")
    loss, outage = codes(r)["link.downlink_loss"], codes(r)["link.downlink_outage"]
    assert loss["loss"] == pytest.approx(5.0, abs=0.1)
    assert outage["seconds"] == pytest.approx(3.01, abs=0.01) and outage["outages"] == 1


def test_a_clean_downlink_has_metrics_and_no_findings(reg: Registry) -> None:
    times = list(range(ARM, DISARM, 100_000))
    f = linked(flight({}, ARM, DISARM), [packets(times, list(range(len(times))))])
    r = LINKS.run(f, reg)
    assert metric(r, "link.downlink_loss") == (0.0, "target")
    assert metric(r, "link.downlink_outage") == (0.1, "target")
    assert codes(r) == {}


# --- uplink and the ground station's silence --------------------------------------------


def mav(rows: Sequence[tuple[int, int, int, int, int]]) -> pa.Table:
    """(time, chan, rxp, rxdp, mgs ms)."""
    return table([r[0] for r in rows], {
        "chan": (pa.uint8(), [r[1] for r in rows]),
        "rxp": (pa.uint16(), [r[2] % 65536 for r in rows]),
        "rxdp": (pa.uint16(), [r[3] % 65536 for r in rows]),
        "mgs": (pa.uint32(), [r[4] for r in rows]),
    })  # fmt: skip


def test_uplink_loss_counts_across_the_counters_wrap_on_links_that_heard_the_gcs(
    reg: Registry,
) -> None:
    rows: list[tuple[int, int, int, int, int]] = []
    for k, t in enumerate(range(ARM, DISARM + 1, 1_000_000)):
        rows.append((t, 0, 65000 + 95 * k, 65500 + 5 * k, t // 1000))  # 5% missed, wraps
        rows.append((t, 1, 100 * k, 50 * k, 0))  # a link that never heard the GCS
    r = LINKS.run(flight({"MAV": mav(rows)}, ARM, DISARM), reg)
    assert metric(r, "link.uplink_loss") == (pytest.approx(5.0), "caution")
    assert codes(r)["link.uplink_loss"] == {"loss": 5.0, "link": 0}
    assert metric(r, "link.gcs_silence_share")[1] == "target"


@pytest.mark.parametrize(("silent_s", "zone", "finding"), [(3, "caution", True), (8, "out", False)])
def test_gcs_silence_is_a_near_miss_short_of_the_failsafes_wait(
    reg: Registry, silent_s: int, zone: str, finding: bool
) -> None:
    last = ARM + 20_000_000  # the GCS last heard, then silent for silent_s
    until = last + silent_s * 1_000_000
    rows = [
        (t, 0, k * 10, 0, (last if last < t <= until else t) // 1000)
        for k, t in enumerate(range(ARM, DISARM, 1_000_000))
    ]
    r = LINKS.run(flight({"MAV": mav(rows)}, ARM, DISARM, {"FS_GCS_TIMEOUT": 5}), reg)
    share, z = metric(r, "link.gcs_silence_share")
    assert z == zone and share == pytest.approx(silent_s / 5, abs=0.01)
    assert ("link.gcs_silence" in codes(r)) is finding


# --- radio status -----------------------------------------------------------------------


def rad(n: int, rssi: float, rem: float, noise: float, remnoise: float,
        txbuf: Sequence[float] | None = None) -> pa.Table:  # fmt: skip
    times = [int(t) for t in np.linspace(ARM, DISARM, n)]
    return table(times, {
        "RSSI": (pa.uint8(), [rssi] * n), "RemRSSI": (pa.uint8(), [rem] * n),
        "TxBuf": (pa.uint8(), list(txbuf) if txbuf is not None else [100] * n),
        "Noise": (pa.uint8(), [noise] * n), "RemNoise": (pa.uint8(), [remnoise] * n),
        "RxErrors": (pa.uint16(), [0] * n), "Fixed": (pa.uint16(), [0] * n),
    })  # fmt: skip


def test_a_full_radio_buffer_slows_every_stream(reg: Registry) -> None:
    buf = [100] * 70 + [30] * 30  # under half free for 30% of the flight
    r = LINKS.run(flight({"RAD": rad(100, 150, 150, 50, 50, buf)}, ARM, DISARM), reg)
    assert metric(r, "link.txbuf_low") == (30.0, "out")
    assert codes(r)["link.txbuf_low"] == {"share": 30.0, "below": "50"}
    assert not [m for m in r.metrics if m.metric == "link.fade_margin"]  # units unknown


def test_sik_fade_margin_and_imbalance_from_the_aircrafts_radio(reg: Registry) -> None:
    # aircraft hears 150 over noise 50 (52.6 dB); the ground 90 over 75 (7.9 dB): weak
    data = {"RAD": rad(100, 150, 90, 50, 75)}
    f = flight(data, ARM, DISARM, answers={"telemetry_radio": "SiK"})
    r = LINKS.run(f, reg)
    margin, zone = metric(r, "link.fade_margin")
    assert zone == "caution" and margin == pytest.approx(15 / 1.9, abs=0.1)
    found = codes(r)["link.fade_margin"]
    assert found["side"] == "ground" and found["range_factor"] == pytest.approx(
        2 ** (7.9 / 6), abs=0.1
    )
    gap, z = metric(r, "link.rssi_asymmetry")
    assert gap == pytest.approx(60 / 1.9, abs=0.1) and z == "out"
    assert codes(r)["link.rssi_asymmetry"]["weaker"] == "ground"


def test_a_sik_radio_known_from_its_own_ids_in_the_tlog(reg: Registry) -> None:
    n = 50
    times = [int(t) for t in np.linspace(ARM, DISARM, n)]
    status = columns(
        TimeUS=pa.array(times, pa.int64()),
        src_sysid=pa.array([SIK[0]] * n, pa.int64()),
        src_compid=pa.array([SIK[1]] * n, pa.int64()),
        rssi=pa.array([200] * n, pa.uint8()),
        remrssi=pa.array([200] * n, pa.uint8()),
        txbuf=pa.array([100] * n, pa.uint8()),
        noise=pa.array([40] * n, pa.uint8()),
        remnoise=pa.array([40] * n, pa.uint8()),
    )
    r = LINKS.run(linked(flight({}, ARM, DISARM), radio=status), reg)
    assert metric(r, "link.fade_margin") == (pytest.approx(160 / 1.9, abs=0.1), "target")
    assert metric(r, "link.rssi_asymmetry") == (0.0, "target")


# --- the RC link ------------------------------------------------------------------------


def rc(
    throttle: Sequence[int], flags: Sequence[int], omask: Sequence[int] | None = None
) -> dict[str, pa.Table]:
    times = [ARM + 100_000 * k for k in range(len(throttle))]
    return {
        "RCIN": table(times, {"C3": (pa.uint16(), list(throttle))}),
        "RCI2": table(
            times,
            {
                "OMask": (pa.uint16(), list(omask or [0] * len(times))),
                "Flags": (pa.uint8(), list(flags)),
            },
        ),
    }


RADIO = {"FS_THR_ENABLE": 1, "FS_THR_VALUE": 975, "RCMAP_THROTTLE": 3}


def test_receiver_rssi_and_link_quality_are_judged_against_the_airframe(reg: Registry) -> None:
    times = [ARM + 100_000 * k for k in range(300)]
    rssi = table(times, {"RXRSSI": (pa.float32(), [0.0] * 20 + [0.8] * 260 + [0.4] * 20),
                         "RXLQ": (pa.float32(), [100.0] * 280 + [60.0] * 20)})  # fmt: skip
    r = LINKS.run(flight({"RSSI": rssi}, ARM, DISARM), reg)
    assert metric(r, "link.rc_rssi") == (pytest.approx(40.0, abs=1), None)  # no fixed zones
    assert metric(r, "link.rc_lq")[0] == pytest.approx(60.0, abs=1)
    assert codes(r) == {}


def test_brief_receiver_failsafes_are_near_misses(reg: Registry) -> None:
    flags = [1] * 100 + [5] * 3 + [1] * 100 + [5] * 5 + [1] * 100
    r = LINKS.run(flight(rc([1500] * len(flags), flags), ARM, DISARM, RADIO), reg)
    assert metric(r, "link.rc_receiver_failsafes") == (2.0, "caution")
    assert codes(r)["link.rc_receiver_failsafes"] == {"count": 2, "longest_s": 0.4}


@pytest.mark.parametrize(
    ("params", "low", "omask", "found"),
    [({}, 980, None, True),  # 5 above FS_THR_VALUE: too close
     ({}, 1000, None, False),  # 25 above: fine
     ({"FS_THR_ENABLE": 0}, 980, None, False),  # the radio failsafe is off
     ({"RC_OPTIONS": 1}, 980, None, False),  # the receiver is ignored
     ({}, 980, [4] * 400, False)],  # the low throttle came over MAVLink
)  # fmt: skip
def test_fs_thr_value_against_the_lowest_throttle_sent(
    reg: Registry, params: dict[str, float], low: int, omask: list[int] | None, found: bool
) -> None:
    throttle = [1500] * 200 + [low] * 100 + [1500] * 100
    data = rc(throttle, [1] * 400, omask)
    r = LINKS.run(flight(data, ARM, DISARM, {**RADIO, **params}), reg)
    assert ("link.rc_fs_thr_margin" in codes(r)) is found
    if found:
        assert codes(r)["link.rc_fs_thr_margin"] == {
            "lowest": 980,
            "value": "975",
            "margin": "5",
            "needed": 10,
        }


def test_nothing_to_judge_and_the_analyzer_is_registered(reg: Registry) -> None:
    assert LINKS.run(flight({}, ARM, DISARM), reg).skipped is not None
    assert LINKS in ANALYZERS
    for code in LINKS.findings:
        assert "{" in reg.findings[code].title["uk"]
    assert reg.rules["links"]["sik"]["sender"] == list(SIK)
