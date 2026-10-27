"""When each failsafe's loss began, whether it fired on time, and losses no failsafe
answered (ArduCopter 4.7.0's waits and check rates, rules/failsafes.json `timing`), on
flights built in memory. The SITL runs that back these numbers: a ground station last
heard at 54.637 s and its failsafe at 59.690 s (FS_GCS_TIMEOUT 5); battery stages 2.1 s
after the voltage went low (BATT_LOW_TIMER 2, checked at 10 Hz)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import table
from test_failsafes import ARM, BATTERY, DISARM, T, build, judged, one

from flight_insights.analyzers import Registry
from flight_insights.analyzers.model import Finding
from flight_insights.knowledge import Pack, load_pack


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


def values(f: Finding) -> dict[str, object]:
    return dict(f.evidence[0].values)


def codes(fs: Sequence[Finding]) -> list[str]:
    return [f.code for f in fs]


# --- the ground station -----------------------------------------------------------------


def mav(last_heard_us: int, end_us: int = DISARM, chan: int = 0) -> pa.Table:
    """A link logged once a second, hearing the GCS every second until `last_heard_us`."""
    times = list(range(ARM - 5_000_000, end_us, 1_000_000))
    mgs = [min(t, last_heard_us) // 1000 for t in times]
    return table(times, {"chan": (pa.uint8(), [chan] * len(times)),
                         "mgs": (pa.uint32(), mgs)})  # fmt: skip


def test_the_gcs_failsafe_counts_from_when_the_gcs_was_last_heard(reg: Registry) -> None:
    heard = T - 5_053_000
    f = one(reg, build("LOITER", [(T, "gcs", 1)], [(T, "RTL", "gcs")], {"FS_GCS_ENABLE": 1},
                       extra={"MAV": mav(heard)}))  # fmt: skip
    assert f.code == "failsafe.as_configured"
    v = values(f)
    assert v["loss_from"] == "the ground station last seen (MAV.mgs)"
    assert (v["waited_s"], v["wait_s"], v["late_s"]) == (5.053, 5.0, 0.0)
    assert "MAV" in f.evidence[0].messages and "mgs" in f.evidence[0].fields


def test_the_newest_link_counts(reg: Registry) -> None:
    links = pa.concat_tables([mav(T - 9_000_000), mav(T - 5_100_000, chan=1)])
    f = one(reg, build("LOITER", [(T, "gcs", 1)], [(T, "RTL", "gcs")], {"FS_GCS_ENABLE": 1},
                       extra={"MAV": links}))  # fmt: skip
    assert values(f)["waited_s"] == 5.1


def test_a_failsafe_well_past_its_wait_is_late(reg: Registry) -> None:
    fs = judged(reg, build("LOITER", [(T, "gcs", 1)], [(T, "RTL", "gcs")],
                           {"FS_GCS_ENABLE": 1, "FS_GCS_TIMEOUT": 5},
                           extra={"MAV": mav(T - 6_500_000)}))  # fmt: skip
    assert codes(fs) == ["failsafe.as_configured", "failsafe.late"]
    late = fs[1]
    assert late.severity == "caution"
    assert late.params["late_s"] == pytest.approx(6.5 - 5 - 0.334 - 0.1, abs=1e-3)
    assert (late.params["waited_s"], late.params["wait_s"]) == (6.5, 5.0)


@pytest.mark.parametrize(
    ("params", "code", "severity"),
    [({"FS_GCS_ENABLE": 1}, "failsafe.missed", "out"),
     ({"FS_GCS_ENABLE": 0}, "failsafe.off_during_loss", "info"),  # the pilot's radio flies it
     ({"FS_GCS_ENABLE": 0, "RC_OPTIONS": 1}, "failsafe.off_during_loss", "safety")],
)  # fmt: skip
def test_a_gcs_lost_with_no_failsafe(
    reg: Registry, params: dict[str, float], code: str, severity: str
) -> None:
    gone = T
    f = one(reg, build("LOITER", [], [], params, extra={"MAV": mav(gone, end_us=T + 20_000_000)}))
    assert (f.code, f.severity) == (code, severity)
    assert (f.params["at_s"], f.params["due_s"]) == (
        round((gone - ARM) / 1e6, 1),
        round((gone + 5_000_000 - ARM) / 1e6, 1),
    )
    assert f.params["what"] == "no ground station heard"


def test_no_loss_when_heard_within_the_wait_or_never_at_all(reg: Registry) -> None:
    short = mav(T, end_us=T + 4_000_000)  # the log ends inside the wait
    assert judged(reg, build("LOITER", [], [], {"FS_GCS_ENABLE": 1}, extra={"MAV": short})) == []
    never = table([ARM, ARM + 1_000_000], {"chan": (pa.uint8(), [0, 0]),
                                           "mgs": (pa.uint32(), [0, 0])})  # fmt: skip
    assert judged(reg, build("LOITER", [], [], {"FS_GCS_ENABLE": 1}, extra={"MAV": never})) == []


def test_a_gcs_failsafe_in_force_from_before_arming_answers_the_loss(reg: Registry) -> None:
    early = mav(ARM - 4_000_000)  # lost on the ground; the failsafe fired, still in force
    fs = judged(reg, build("LOITER", [(ARM - 1_000_000 + 2, "gcs", 1)], [], {"FS_GCS_ENABLE": 1},
                           extra={"MAV": early}))  # fmt: skip
    assert codes(fs) == []


# --- the battery ------------------------------------------------------------------------


def bat(volt: Sequence[float], used: Sequence[float] | None = None) -> pa.Table:
    """BAT at 10 Hz from arming, one monitor."""
    times = [ARM + 100_000 * k for k in range(len(volt))]
    cols: dict[str, tuple[pa.DataType, Sequence[object]]] = {
        "Inst": (pa.uint8(), [0] * len(times)),
        "Volt": (pa.float32(), list(volt)),
        "VoltR": (pa.float32(), [v + 0.4 for v in volt]),
        "H": (pa.uint8(), [1] * len(times)),
    }
    if used is not None:
        cols["CurrTot"] = (pa.float32(), list(used))
    return table(times, cols)


def at(k: int) -> int:
    return ARM + 100_000 * k


LOW = {**BATTERY, "BATT_LOW_VOLT": 11.0, "BATT_CRT_VOLT": 9.0, "BATT_LOW_TIMER": 2}


def test_a_battery_stage_counts_from_its_first_low_sample(reg: Registry) -> None:
    volts = [12.0] * 300 + [10.5] * 300  # low from sample 300
    fired = at(321)  # the first check more than 2 s on
    f = one(reg, build("LOITER", [(fired, "batt", 1)], [(fired, "RTL", "batt")], LOW,
                       extra={"BAT": bat(volts)}))  # fmt: skip
    v = values(f)
    assert v["loss_from"] == "Volt under BATT_LOW_VOLT 11 V"
    assert (v["waited_s"], v["wait_s"], v["late_s"]) == (2.1, 2.0, 0.0)


def test_sag_compensated_voltage_where_the_settings_say(reg: Registry) -> None:
    volts = [12.0] * 300 + [10.4] * 300  # VoltR 10.8: under 11 too
    fired = at(321)
    f = one(reg, build("LOITER", [(fired, "batt", 1)], [(fired, "RTL", "batt")],
                       {**LOW, "BATT_FS_VOLTSRC": 1}, extra={"BAT": bat(volts)}))  # fmt: skip
    assert values(f)["loss_from"] == "VoltR under BATT_LOW_VOLT 11 V"
    assert "VoltR" in f.evidence[0].fields


def test_a_capacity_stage_acts_at_once(reg: Registry) -> None:
    used = list(np.linspace(0, 6000, 600))  # past 5000 - 1000 reserve at sample 400
    params = {**LOW, "BATT_CAPACITY": 5000, "BATT_LOW_MAH": 1000}
    first = next(k for k, u in enumerate(used) if 5000 - u < 1000)
    fired = at(first)
    f = one(reg, build("LOITER", [(fired, "batt", 1)], [(fired, "RTL", "batt")], params,
                       extra={"BAT": bat([12.0] * 600, used)}))  # fmt: skip
    v = values(f)
    assert v["loss_from"] == "over 4000 mAh used"
    assert (v["waited_s"], v["wait_s"], v["late_s"]) == (0.0, 0.0, 0.0)


def test_a_battery_low_past_its_wait_with_no_failsafe(reg: Registry) -> None:
    volts = [12.0] * 300 + [10.5] * 100
    f = one(reg, build("LOITER", [], [], LOW, extra={"BAT": bat(volts)}))
    assert (f.code, f.severity) == ("failsafe.missed", "out")
    assert f.params["what"] == "Volt under BATT_LOW_VOLT 11 V" and f.params["due_s"] == 32.0
    short = [12.0] * 300 + [10.5] * 15 + [12.0] * 100  # 1.5 s low: inside the wait
    assert judged(reg, build("LOITER", [], [], LOW, extra={"BAT": bat(short)})) == []


def test_a_battery_low_timer_of_zero_switches_the_voltage_failsafe_off(reg: Registry) -> None:
    volts = [12.0] * 300 + [10.5] * 200
    f = one(reg, build("LOITER", [], [], {**LOW, "BATT_LOW_TIMER": 0},
                       extra={"BAT": bat(volts)}))  # fmt: skip
    assert (f.code, f.severity, f.params["setting"]) == (
        "failsafe.off_during_loss",
        "out",
        "BATT_LOW_TIMER 0",
    )


# --- the radio --------------------------------------------------------------------------


def rcin(throttle: Sequence[int], flags: Sequence[int] | None = None,
         omask: Sequence[int] | None = None) -> dict[str, pa.Table]:  # fmt: skip
    """RCIN and RCI2 at 10 Hz from arming."""
    times = [at(k) for k in range(len(throttle))]
    flags = list(flags) if flags is not None else [1] * len(times)
    return {
        "RCIN": table(times, {"C3": (pa.uint16(), list(throttle))}),
        "RCI2": table(
            times,
            {"OMask": (pa.uint16(), list(omask or [0] * len(times))), "Flags": (pa.uint8(), flags)},
        ),
    }


RADIO = {"FS_THR_ENABLE": 1, "FS_THR_VALUE": 975, "RC3_MIN": 1000, "RCMAP_THROTTLE": 3}


def test_a_throttle_failsafe_counts_from_the_low_throttle(reg: Registry) -> None:
    fired = at(300) + 57_000  # tripped between two logged samples, as in SITL
    f = one(reg, build("LOITER", [(fired, "radio", 1)], [(fired, "RTL", "radio")], RADIO,
                       extra=rcin([1500] * 301 + [950] * 100)))  # fmt: skip
    v = values(f)
    assert v["loss_from"] == "the throttle RCIN.C3 under FS_THR_VALUE 975"
    assert (v["waited_s"], v["late_s"]) == (0.0, 0.0)


def err_radio(t: int) -> list[tuple[int, str, int]]:
    return [(t, "radio_late", 2), (t, "radio", 1)]


@pytest.fixture(autouse=True)
def radio_subsystem(monkeypatch: pytest.MonkeyPatch) -> None:
    import test_failsafes

    monkeypatch.setitem(test_failsafes.SUBSYS, "radio_late", 2)  # ERR RADIO, late frame


def test_a_receiver_that_flags_failsafe_counts_from_its_flag(reg: Registry) -> None:
    fired = at(310)  # RC_FS_TIMEOUT 1 s after the flag at sample 300
    flags = [1] * 300 + [4] * 200
    f = one(reg, build("LOITER", err_radio(fired), [(fired, "RTL", "radio")], RADIO,
                       extra=rcin([1500] * 500, flags)))  # fmt: skip
    v = values(f)
    assert v["loss_from"] == "the receiver's failsafe flag (RCI2.Flags)"
    assert (v["waited_s"], v["wait_s"], v["late_s"]) == (1.0, 1.0, 0.0)


def test_a_receiver_that_just_stops_leaves_no_onset(reg: Registry) -> None:
    fired = at(310)
    f = one(reg, build("LOITER", err_radio(fired), [(fired, "RTL", "radio")], RADIO,
                       extra=rcin([1500] * 500)))  # fmt: skip
    assert values(f)["loss_from"] is None and values(f)["late_s"] is None


@pytest.mark.parametrize(
    ("params", "code", "severity"),
    [({"FS_THR_ENABLE": 0}, "failsafe.off_during_loss", "safety"),
     ({}, "failsafe.missed", "out")],
)  # fmt: skip
def test_a_receiver_lost_with_no_failsafe(
    reg: Registry, params: dict[str, float], code: str, severity: str
) -> None:
    lost = rcin([1500] * 300 + [950] * 60 + [1500] * 100)
    f = one(reg, build("LOITER", [], [], {**RADIO, **params}, extra=lost))
    assert (f.code, f.severity) == (code, severity)
    assert f.params["what"] == "the throttle under FS_THR_VALUE 975" and f.params["lasted_s"] == 5.9


@pytest.mark.parametrize(
    "params",
    [
        {"RC_OPTIONS": 1},  # the receiver is ignored: the ground station flies it
        {"RC3_MIN": 980},
    ],  # FS_THR_VALUE too near the stick's lowest to tell
)
def test_a_low_throttle_that_says_nothing(reg: Registry, params: dict[str, float]) -> None:
    lost = rcin([1500] * 300 + [950] * 60 + [1500] * 100)
    assert judged(reg, build("LOITER", [], [], {**RADIO, "FS_THR_ENABLE": 0, **params},
                             extra=lost)) == []  # fmt: skip


def test_an_overridden_throttle_is_not_the_receivers(reg: Registry) -> None:
    lost = rcin([1500] * 300 + [950] * 60 + [1500] * 100, omask=[0] * 300 + [4] * 160)
    assert judged(reg, build("LOITER", [], [], {**RADIO, "FS_THR_ENABLE": 0}, extra=lost)) == []


def test_a_receiver_flag_held_past_the_timeout_with_the_failsafe_off(reg: Registry) -> None:
    flags = [1] * 300 + [5] * 40 + [1] * 100
    f = one(reg, build("LOITER", [], [], {**RADIO, "FS_THR_ENABLE": 0},
                       extra=rcin([1500] * 440, flags)))  # fmt: skip
    assert (f.code, f.severity, f.params["what"]) == (
        "failsafe.off_during_loss",
        "safety",
        "the receiver in failsafe",
    )
    ignored = {**RADIO, "FS_THR_ENABLE": 0, "RC_OPTIONS": 4}  # the flag is ignored
    assert judged(reg, build("LOITER", [], [], ignored, extra=rcin([1500] * 440, flags))) == []
