"""The Links view (links_view.py, flight.links): the link analyzer's measures laid out
over time in seconds after arming, and the failsafe settings for the link with what
losing it does in each mode. On flights built in memory, then on the 4.7.0 SITL link
flights imported for real, whose replies must hold no position, system ID or absolute
time, and read nothing the location policy denies."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from flight_builder import ARM, DISARM, flight, table
from test_links import linked, mav, packets, rad

from flight_insights.ai.policy import Policy
from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.links import LINKS
from flight_insights.knowledge import Pack, load_pack
from flight_insights.links_view import READS, SETTINGS, failsafes, links

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/sitl/4.7.0"
STATION = {"RC_OPTIONS": 1, "FS_THR_ENABLE": 1, "RC_FS_TIMEOUT": 1, "FS_GCS_ENABLE": 1,
           "FS_GCS_TIMEOUT": 5, "FS_OPTIONS": 16, "RC_OVERRIDE_TIME": 3}  # fmt: skip


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack()


@pytest.fixture(scope="module")
def reg(pack: Pack) -> Registry:
    return Registry.load(pack)


def evidence(f: FlightData, reg: Registry, eid: str) -> dict[str, Any]:
    [found] = [e for e in LINKS.run(f, reg).evidence if e.id == eid]
    return dict(found.values)


# --- the measures over time -------------------------------------------------------------------


def test_the_downlink_shows_each_silence_and_the_outages_the_analyzer_counts(
    pack: Pack, reg: Registry,
) -> None:  # fmt: skip
    t = list(range(ARM, DISARM, 100_000))
    t = [*t[:100], *[x + 3_000_000 for x in t[100:]]]  # 3 s heard nothing, 11 s in
    t = [x for x in t if x <= DISARM]
    f = linked(flight({}, ARM, DISARM), [packets(t, list(range(len(t))))])
    view = links(f, pack)
    [station] = view["downlink"]["stations"]
    assert view["downlink"]["outage_s"] == 2.0 and station["outages"] == 1
    assert station["loss_pct"] == evidence(f, reg, "link:downlink")["loss_pct"] == 0.0
    [outage] = station["outage_list"]
    assert outage["from_s"] == pytest.approx(9.9) and outage["to_s"] == pytest.approx(13.0)
    assert max(station["silence"]["hi"]) == pytest.approx(3.1) == station["longest_s"]
    assert min(station["silence"]["t"]) >= 0  # seconds after arming
    assert station["clock"] == "unconfirmed" and station["delay_ms"] is None


def test_the_uplink_shows_how_long_the_ground_station_went_unheard(
    pack: Pack, reg: Registry,
) -> None:  # fmt: skip
    last, until = ARM + 20_000_000, ARM + 23_000_000  # heard last 20 s in, silent 3 s
    rows = [(t, 0, k * 10, k // 20, (last if last < t <= until else t) // 1000)
            for k, t in enumerate(range(ARM, DISARM, 1_000_000))]  # fmt: skip
    f = flight({"MAV": mav(rows)}, ARM, DISARM, {"FS_GCS_TIMEOUT": 5})
    up = links(f, pack)["uplink"]
    judged = evidence(f, reg, "link:gcs_silence")
    assert (up["longest_s"], up["from_s"]) == (judged["longest_s"], judged["from_s"]) == (3, 20)
    assert up["timeout_s"] == 5 and max(up["unheard"]["hi"]) == 3
    assert up["links"] == [{"chan": 0, "received": 380, "missed": 1, "loss_pct": 0.26}]


def test_joystick_lapses_are_the_stretches_with_no_override_in_force(pack: Pack) -> None:
    times = [ARM + 100_000 * k for k in range(390)]
    omask = [15] * 100 + [0] * 40 + [15] * 250  # no command held for 4 s, 10 s in
    rci2 = table(times, {"OMask": (pa.uint16(), omask), "Flags": (pa.uint8(), [1] * 390)})
    view = links(flight({"RCI2": rci2}, ARM, DISARM, STATION), pack)
    joy = view["joystick"]
    assert view["control"] == "station" and joy["override_time_s"] == 3
    assert joy["held_pct"] == pytest.approx(100 * 350 / 390, abs=0.1)
    assert joy["lapses"] == [{"from_s": 10.0, "to_s": 13.9}]
    assert view["receiver"] == {"ignored": True}  # the station flies it
    pilot = links(flight({"RCI2": rci2}, ARM, DISARM, {"RC_OPTIONS": 0}), pack)
    assert pilot["joystick"] is not None  # overrides sent, so shown however it is flown
    quiet = table(times, {"OMask": (pa.uint16(), [0] * 390), "Flags": (pa.uint8(), [1] * 390)})
    assert links(flight({"RCI2": quiet}, ARM, DISARM), pack)["joystick"] is None


def test_fade_margin_is_drawn_for_sik_radios_only(pack: Pack) -> None:
    data = {"RAD": rad(100, 150, 90, 50, 75, [100] * 60 + [30] * 40)}
    sik = links(flight(data, ARM, DISARM, answers={"telemetry_radio": "SiK"}), pack)["radio"]
    assert sik["reported_by"] == "aircraft" and sik["sik"] and sik["target_db"] == 12
    assert sik["slows_streams"] == 50 and min(sik["txbuf"]["lo"]) == 30
    assert max(sik["fade_db"]["aircraft"]["hi"]) == pytest.approx(100 / 1.9, abs=0.01)
    assert max(sik["fade_db"]["ground"]["hi"]) == pytest.approx(15 / 1.9, abs=0.01)
    other = links(flight(data, ARM, DISARM, answers={"telemetry_radio": "Doodle Labs"}), pack)
    assert not other["radio"]["sik"] and "fade_db" not in other["radio"]
    assert other["radio_kind"] == "Doodle Labs"


def test_the_receivers_signal_and_its_own_failsafes(pack: Pack) -> None:
    times = [ARM + 100_000 * k for k in range(300)]
    rssi = table(times, {"RXRSSI": (pa.float32(), [0.0] * 20 + [0.8] * 280),
                         "RXLQ": (pa.float32(), [0.0] * 300)})  # fmt: skip
    flags = [1] * 100 + [5] * 3 + [1] * 197
    rci2 = table(times, {"OMask": (pa.uint16(), [0] * 300), "Flags": (pa.uint8(), flags)})
    rx = links(flight({"RSSI": rssi, "RCI2": rci2}, ARM, DISARM), pack)["receiver"]
    assert rx["ignored"] is False and "lq" not in rx  # a receiver that reports none
    assert min(rx["rssi"]["lo"]) == pytest.approx(80.0) and min(rx["rssi"]["t"]) >= 2
    assert rx["failsafes"] == [{"from_s": 10.0, "to_s": 10.2}]


# --- the failsafe settings for the link -------------------------------------------------------


def rows(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["param"]: r for r in view["settings"]}


def test_flown_from_the_station_the_radio_failsafe_acts_first(pack: Pack, reg: Registry) -> None:
    view = failsafes(flight({}, ARM, DISARM, STATION, defaults={"RC_OVERRIDE_TIME": 3}), reg,
                     pack, {"longest_s": 1.0})  # fmt: skip
    got = rows(view)
    assert view["control"] == "station"
    assert (got["FS_THR_ENABLE"]["status"], got["FS_THR_ENABLE"]["verdict"]) == ("ok", "first")
    assert got["FS_THR_ENABLE"]["action"] == "RTL" and got["FS_THR_ENABLE"]["wait_s"] == 1
    assert (got["FS_GCS_ENABLE"]["status"], got["FS_GCS_ENABLE"]["verdict"]) == ("ok", "backstop")
    assert got["FS_GCS_TIMEOUT"] | {} == {
        "param": "FS_GCS_TIMEOUT",
        "value": 5,
        "longest_s": 1.0,
        "margin_s": 4.0,
        "status": "ok",
        "verdict": "margin",
    }
    assert got["FS_OPTIONS"]["options"] == ["GCS_CONTINUE_IF_PILOT_CONTROL"]
    assert got["RC_OVERRIDE_TIME"]["verdict"] == "default"
    [loss] = view["losses"]
    assert loss["id"] == "link_lost" and loss["status"] == "ok" and loss["alone"] == []
    assert loss["lines"][0]["en"].startswith("fly home and land (RTL) after 1 s")
    assert loss["lines"][0]["uk"]


def test_with_the_radio_failsafe_off_a_pilot_flown_mode_is_left_with_no_one_flying_it(
    pack: Pack, reg: Registry,
) -> None:  # fmt: skip
    view = failsafes(flight({}, ARM, DISARM, STATION | {"FS_THR_ENABLE": 0}), reg, pack, None)
    got = rows(view)
    assert (got["FS_THR_ENABLE"]["status"], got["FS_THR_ENABLE"]["verdict"]) == ("out", "off")
    assert got["FS_GCS_ENABLE"]["verdict"] == "first"
    assert got["FS_GCS_TIMEOUT"]["verdict"] == "unmeasured"
    assert got["RC_OVERRIDE_TIME"]["verdict"] == "value"  # the log holds no default
    [loss] = view["losses"]
    assert loss["status"] == "out" and loss["alone"] == ["LOITER", "POSHOLD", "ALT_HOLD"]
    assert loss["alone_names"] == ["Loiter", "PosHold", "AltHold"]


@pytest.mark.parametrize(
    ("params", "silent", "gcs", "timeout"),
    [({"FS_GCS_ENABLE": 0}, None, ("info", "off_pilot"), None),  # the pilot still flies it
     ({"FS_GCS_ENABLE": 1}, 3.0, ("ok", "on"), ("caution", "margin")),  # a near miss
     ({"FS_GCS_ENABLE": 1}, 9.0, ("ok", "on"), ("info", "reached"))],  # the failsafe's to judge
)  # fmt: skip
def test_with_a_pilots_radio_the_ground_station_failsafe_is_the_telemetrys(
    pack: Pack, reg: Registry, params: dict[str, float], silent: float | None,
    gcs: tuple[str, str], timeout: tuple[str, str] | None,
) -> None:  # fmt: skip
    view = failsafes(flight({}, ARM, DISARM, params), reg, pack, {"longest_s": silent})
    got = rows(view)
    assert view["control"] == "receiver" and "RC_OVERRIDE_TIME" not in got
    assert (got["FS_GCS_ENABLE"]["status"], got["FS_GCS_ENABLE"]["verdict"]) == gcs
    if timeout is None:
        assert "FS_GCS_TIMEOUT" not in got
    else:
        assert (got["FS_GCS_TIMEOUT"]["status"], got["FS_GCS_TIMEOUT"]["verdict"]) == timeout
    assert [loss["id"] for loss in view["losses"]] == ["radio_lost", "telemetry_lost"]
    assert all(loss["status"] == "ok" for loss in view["losses"])  # the radio failsafe is on


def test_the_failsafe_settings_are_modelled_for_copter_only(pack: Pack, reg: Registry) -> None:
    plane = flight({}, ARM, DISARM, fingerprint={"vehicle": "plane", "board": {}})
    assert failsafes(plane, reg, pack, None) == {"skipped": "copter_only"}


# --- the SITL link flights, over RPC ---------------------------------------------------------


@pytest.fixture(scope="module")
def replies(
    tmp_path_factory: pytest.TempPathFactory, pack: Pack,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:  # fmt: skip
    """Each flight's flight.links reply and its analysis."""
    from test_rpc import LAPTOP, Output, call, keys

    from flight_insights.rpc import build_server

    out = Output()
    server = build_server(out, pack)
    n = iter(range(1, 10**6))
    folder = tmp_path_factory.mktemp("links-view")
    call(server, out, next(n), "engine.unlock", laptop_id=LAPTOP, data_dir=str(folder), keys=keys())
    pl = call(server, out, next(n), "platform.create", name="Q", vehicle="copter")["result"]
    af = call(server, out, next(n), "airframe.create", platform_id=pl["id"], name="Q-1")["result"]
    got: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for name, files in (("copter-quad-fs-gcs", (".bin", ".tlog")),
                        ("copter-quad-fs-off", (".bin", ".tlog")),
                        ("copter-quad-hover", (".bin",))):  # fmt: skip
        paths = [str(FIXTURES / f"{name}{x}") for x in files]
        scanned = call(server, out, next(n), "import.scan", paths=paths)["result"]
        done = call(server, out, next(n), "import.commit", batch=scanned["batch"],
                    decisions=[{"log": "f1", "airframe_id": af["id"]}])["result"]  # fmt: skip
        fid = done[0]["flights"][0]  # the onboard log's; a tlog is linked to it
        analysis = call(server, out, next(n), "analysis.run", flight_id=fid)["result"]
        view = call(server, out, next(n), "flight.links", flight_id=fid)["result"]
        got[name] = (view, analysis)
    missing = call(server, out, next(n), "flight.links", flight_id="no-such-flight")
    assert missing["error"]["code"] == -32003  # BAD_INPUT
    return got


def walk(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for k, v in value.items():  # type: ignore[reportUnknownVariableType]
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for v in value:  # type: ignore[reportUnknownVariableType]
            yield from walk(v, f"{path}[]")
    else:
        yield path, value


POSITION = re.compile(
    r"(?i)(^|_)(lat|lon|lng|latitude|longitude|alt|altitude|amsl|home|origin)($|_)"
)
NOT_SENT = {"sysid", "compid", "offset_us", "drift_ppm", "source_file_id", "utc_us",
            "boot_utc_us", "first_record", "last_record"}  # fmt: skip
WHEN = re.compile(r"(\.t\[\]|\.(from_s|to_s|longest_at_s|t))$")  # a moment, not a duration


@pytest.mark.parametrize("name", ["copter-quad-fs-gcs", "copter-quad-fs-off", "copter-quad-hover"])
def test_the_reply_holds_no_position_system_id_or_absolute_time(
    replies: dict[str, tuple[dict[str, Any], dict[str, Any]]], pack: Pack, name: str,
) -> None:  # fmt: skip
    view, _ = replies[name]
    lo, hi = view["window_s"]
    for path, value in walk(view):
        keys = re.findall(r"\.(\w+)", path)
        assert not any(POSITION.search(k) or k in NOT_SENT for k in keys), path
        if WHEN.search(path) and isinstance(value, int | float):
            assert lo - 1e-6 <= value <= hi + 1e-6, (path, value)  # seconds after arming
        if isinstance(value, int | float) and not isinstance(value, bool):
            assert abs(value) < 1e6, (path, value)  # no boot or wall-clock microseconds
    policy = Policy.load(pack, "4.7.0")
    for message, fields in READS.items():
        for f in fields:
            assert policy.field(message, f).verdict == "allow", (message, f)
    assert not any(policy.parameter_removed(p) for p in SETTINGS)


def test_the_view_agrees_with_the_analysis(
    replies: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:  # fmt: skip
    view, analysis = replies["copter-quad-fs-gcs"]
    ev = {e["id"]: e["values"] for e in analysis["evidence"]}
    [station] = view["downlink"]["stations"]
    assert station["loss_pct"] == ev["link:downlink"]["loss_pct"]
    assert station["longest_s"] == ev["link:downlink"]["longest_s"]
    assert station["delay_ms"] is not None and len(station["delay_ms"]) == 2
    up = view["uplink"]
    assert (up["longest_s"], up["from_s"]) == (ev["link:gcs_silence"]["longest_s"],
                                               ev["link:gcs_silence"]["from_s"])  # fmt: skip
    unheard = up["unheard"]
    armed = [
        v for t, v in zip(unheard["t"], unheard["hi"], strict=True) if 0 <= t <= view["armed_s"]
    ]
    assert max(armed) == pytest.approx(up["longest_s"], abs=0.01)  # drawn past disarming too
    got = rows(view["failsafes"])
    assert got["FS_GCS_TIMEOUT"]["verdict"] == "reached"  # lost for good: the failsafe fired
    assert [m["mode"] for m in view["modes"]] == ["GUIDED", "RTL"]
    off, _ = replies["copter-quad-fs-off"]
    radio_lost = off["failsafes"]["losses"][0]
    assert radio_lost["id"] == "radio_lost"
    assert radio_lost["alone"] == ["LOITER", "POSHOLD", "ALT_HOLD"]
    hover, _ = replies["copter-quad-hover"]
    assert hover["tlogs"] == 0 and hover["downlink"] is None and hover["uplink"] is not None
    assert np.isfinite(hover["armed_s"])
