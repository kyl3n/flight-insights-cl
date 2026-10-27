"""What the Links tab draws for one flight (spec: Links, ground station and whole-system
faults, "The Links tab"; mock-up Links), from what ArduPilot and the ground station
recorded:

- **Downlink:** each station's view of the vehicle's packets while armed, the silence
  before each packet, and the silences long enough to count as outages.
- **Uplink:** each link's packets from the ground the autopilot received and knows it
  missed, and how long the ground station had gone unheard (MAV.mgs), against
  FS_GCS_TIMEOUT.
- **Joystick:** when the ground station flies the aircraft, the stretches the autopilot
  held no RC override from it (RCI2.OMask): each began at least RC_OVERRIDE_TIME after
  the joystick's last command.
- **Radio status** (RAD, else the tlog's RADIO_STATUS): the TX buffer, and the fade
  margin at each end for SiK firmware radios only, the only ones whose units are known.
- **RC receiver:** RSSI and link quality, and the receiver's own failsafes.
- **Failsafe settings for the link:** each setting with what the firmware does with it,
  and what losing the link does in each mode it may be flown in, played through the
  firmware's own failsafes as the recovery card plays them (recovery_link.py) and worded
  by the card's own phrases.

The measures are the link analyzer's own (analyzers/links.py): this module only lays
them out over time. Traces are reduced for display as every flight view's are. Times are
seconds after arming; nothing here reads a position, a system ID, the station's clock
offset or any absolute time, and the reply never leaves the laptop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.links import (
    IGNORE_RECEIVER,
    PROTOCOL_FAILSAFE,
    RADIO_FIELDS,
    downlink_views,
    fade_margins,
    is_sik,
    mav_rows,
    radio_table,
    rci2,
    runs_of,
    unheard,
    uplink_links,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.knowledge import Pack
from flight_insights.recovery import Card
from flight_insights.recovery_link import (
    GCS,
    RADIO,
    STARTS,
    State,
    alone,
    by_mode,
    play,
    sequence,
)
from flight_insights.views import envelope

Array = np.ndarray[Any, Any]
MARGIN_S = 5.0  # drawn either side of the armed span, as the other flight views

# What the view reads from the onboard log, for the location policy's check: none of it
# is a position. Tlog packets are read by sender and sequence only, and RADIO_STATUS for
# the same five fields as RAD.
READS: Mapping[str, tuple[str, ...]] = {
    "MAV": ("chan", "rxp", "rxdp", "mgs"),
    "RAD": RADIO_FIELDS["aircraft"],
    "RSSI": ("RXRSSI", "RXLQ"),
    "RCI2": ("Flags", "OMask"),
    "MODE": ("ModeNum",),
}
SETTINGS = ("RC_OPTIONS", "FS_THR_ENABLE", "RC_FS_TIMEOUT", "FS_GCS_ENABLE", "FS_GCS_TIMEOUT",
            "FS_OPTIONS", "RC_OVERRIDE_TIME")  # fmt: skip


def _floats(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.float64)


def _param(flight: FlightData, name: str, fallback: float) -> float:
    return float(flight.params.get(name, flight.defaults.get(name, fallback)))


class _Clock:
    """Seconds after arming, and traces over the view's window."""

    def __init__(self, flight: FlightData) -> None:
        self.flight = flight
        self.armed_s = round((flight.disarm_us - flight.arm_us) / 1e6, 3)
        self.window = (-MARGIN_S, self.armed_s + MARGIN_S)

    def after(self, t_us: float) -> float:
        return round((t_us - self.flight.arm_us) / 1e6, 3)

    def trace(self, t_us: Array, values: Array) -> dict[str, list[float]]:
        return envelope((t_us - self.flight.arm_us) / 1e6, values, *self.window)

    def armed(self, t_us: Array) -> Array:
        return (t_us >= self.flight.arm_us) & (t_us <= self.flight.disarm_us)

    def spans(self, t_us: Array, flagged: Array) -> list[dict[str, float]]:
        """Each run of flagged samples as seconds after arming."""
        return [{"from_s": self.after(float(t_us[a])), "to_s": self.after(float(t_us[b]))}
                for a, b in runs_of(flagged)]  # fmt: skip


def _modes(flight: FlightData, pack: Pack, clock: _Clock) -> list[dict[str, Any]]:
    """Mode changes inside the window, by the release's own mode names."""
    table = flight.table("MODE", ["TimeUS", "ModeNum"])
    if table is None or not table.num_rows or not flight.firmware:
        return []
    names: Mapping[str, str] = pack.enums(flight.firmware)["modes"].get(flight.vehicle, {})
    out: list[dict[str, Any]] = []
    for t_us, n in zip(_floats(table, "TimeUS"), _floats(table, "ModeNum"), strict=True):
        name = names.get(str(int(n)), f"MODE_{int(n)}")
        t = clock.after(float(t_us))
        if clock.window[0] <= t <= clock.window[1] and (not out or out[-1]["mode"] != name):
            out.append({"t": t, "mode": name})
    return out


# --- the downlink and the uplink --------------------------------------------------------------


def _downlink(flight: FlightData, rules: Mapping[str, Any], clock: _Clock) -> dict[str, Any] | None:
    """Each station's view while armed, as the analyzer counts it, with the silence
    before each packet and the silences that count as outages."""
    outage_s = float(rules["outage_s"])
    found = downlink_views(flight, outage_s)
    if not found:
        return None
    stations: list[dict[str, Any]] = []
    for view, times, session in found:
        gaps = np.diff(times) / 1e6
        long = np.flatnonzero(gaps > outage_s)
        delay = session.get("quality", {}).get("delay_ms")
        stations.append(view | {
            "clock": str(session.get("status", "unconfirmed")),
            "delay_ms": [float(v) for v in delay] if delay else None,
            "silence": clock.trace(times[1:], gaps),
            "outage_list": [{"from_s": clock.after(float(times[k])),
                             "to_s": clock.after(float(times[k + 1]))} for k in long],
        })  # fmt: skip
    return {"outage_s": outage_s, "stations": stations}


def _uplink(flight: FlightData, clock: _Clock) -> dict[str, Any] | None:
    """Each link that heard the ground station, and how long it had gone unheard as each
    MAV row was logged, with the longest while armed as the analyzer judges it."""
    rows = mav_rows(flight)
    if rows is None:
        return None
    times, chan, rxp, rxdp, mgs = rows
    out: dict[str, Any] = {"links": uplink_links(flight, times, chan, rxp, rxdp, mgs)}
    if mgs is None:
        return out
    silent, heard = unheard(times, chan, mgs)
    if not np.any(heard):
        return out
    out["timeout_s"] = _param(flight, "FS_GCS_TIMEOUT", 5.0)
    out["unheard"] = clock.trace(times[heard], silent[heard])
    armed = clock.armed(times) & heard
    if np.any(armed):
        k = int(np.argmax(silent[armed]))
        longest = round(float(silent[armed][k]), 2)
        out["longest_s"] = longest
        out["from_s"] = round(clock.after(float(times[armed][k])) - longest, 1)
    return out


def _joystick(flight: FlightData, station: bool, clock: _Clock) -> dict[str, Any] | None:
    """When the ground station flies it, or sent overrides at all: the share of the armed
    time an RC override from it was in force, and the stretches with none."""
    found = rci2(flight)
    if found is None:
        return None
    times, _, omask = found
    armed = clock.armed(times)
    if not np.any(armed) or not (station or np.any(omask[armed] != 0)):
        return None
    t, held = times[armed], np.asarray(omask[armed] != 0, bool)
    return {
        "held_pct": round(100.0 * float(np.count_nonzero(held)) / len(held), 1),
        "lapses": clock.spans(t, ~held),
        "override_time_s": _param(flight, "RC_OVERRIDE_TIME", 3.0),
    }


# --- the radios and the receiver ----------------------------------------------------------------


def _radio(flight: FlightData, reg: Registry, rules: Mapping[str, Any], clock: _Clock
           ) -> dict[str, Any] | None:  # fmt: skip
    found = radio_table(flight)
    if found is None:
        return None
    table, end = found
    names = RADIO_FIELDS[end]
    if any(n not in table.column_names for n in names):
        return None
    t = _floats(table, "TimeUS")
    known = np.isfinite(t)  # a tlog packet the session's clock could not place
    t = t[known]
    rssi, rem, txbuf, noise, remnoise = (_floats(table, n)[known] for n in names)
    sik = is_sik(flight, rules)
    out: dict[str, Any] = {
        "reported_by": end,
        "sik": sik,
        "slows_streams": float(rules["txbuf"]["slows_streams"]),
        "txbuf": clock.trace(t, txbuf),
    }
    if sik:
        margins = fade_margins(end, rssi, rem, noise, remnoise, float(rules["sik"]["db_per_unit"]))
        out["fade_db"] = {side: clock.trace(t[counted], v[counted])
                          for side, (counted, v) in margins.items() if np.any(counted)}  # fmt: skip
        target = reg.metrics["link.fade_margin"].zones.get("target", ())
        out["target_db"] = target[0][0] if target else None
    return out


def _receiver(flight: FlightData, station: bool, clock: _Clock) -> dict[str, Any]:
    """The receiver's RSSI and link quality (percent, 0 where it reports none) and its
    own failsafes while armed; nothing when RC_OPTIONS tells ArduPilot to ignore it."""
    if station:
        return {"ignored": True}
    out: dict[str, Any] = {"ignored": False}
    table = flight.table("RSSI")
    if table is not None and table.num_rows:
        t = _floats(table, "TimeUS")
        for field, key, scale in (("RXRSSI", "rssi", 100.0), ("RXLQ", "lq", 1.0)):
            if field in table.column_names:
                v = _floats(table, field) * scale
                if np.any(v > 0):
                    out[key] = clock.trace(t[v > 0], v[v > 0])
    found = rci2(flight)
    if found is not None:
        times, flags, _ = found
        armed = clock.armed(times)
        out["failsafes"] = clock.spans(times[armed], (flags[armed] & PROTOCOL_FAILSAFE) > 0)
    return out


# --- the failsafe settings for the link ------------------------------------------------------


def _action(rules: Mapping[str, Any], kind: str, value: float) -> str:
    rule: Mapping[str, Any] = rules["failsafes"][kind]
    return str(rule["actions"].get(str(int(value)), rule["otherwise"]))


def failsafes(flight: FlightData, reg: Registry, pack: Pack, uplink: Mapping[str, Any] | None
              ) -> dict[str, Any]:  # fmt: skip
    """Each link failsafe setting with its status, and what losing the link does in each
    mode it may be flown in. A setting the log did not hold is the firmware's default.

    - **Radio failsafe** (FS_THR_ENABLE): off is out of range, as the configuration
      review holds it. Flown from the ground station, the joystick's commands count as
      radio input, so it acts first when its wait is the shorter.
    - **Ground-station failsafe** (FS_GCS_ENABLE): off is out of range when the station
      flies it (the telemetry link is its only control link), and only noted with a
      pilot's radio, which still flies it. On, it is the backstop behind a quicker radio
      failsafe.
    - **FS_GCS_TIMEOUT** against this flight's longest silence from the station while
      armed, in the zones of `link.gcs_silence_share`; a silence that reached it is the
      failsafe analysis's to judge.
    - **FS_OPTIONS** and **RC_OVERRIDE_TIME** are listed, not judged: what they do shows
      in the losses played below them.
    - **Losses:** one for a ground station that flies it, two for a pilot's radio (the
      radio, and the telemetry with the radio working), as the recovery card has them; a
      loss after which some mode is left with no one flying it is out of range."""
    if flight.vehicle != "copter":
        return {"skipped": "copter_only"}
    rules: Mapping[str, Any] = reg.rules["failsafes"]
    doc: Mapping[str, Any] = pack.json("recovery/copter.json")
    c = Card(doc)
    p: dict[str, float] = (
        dict(doc["link"]["defaults"]) | dict(flight.defaults) | dict(flight.params)
    )
    station = bool(int(p.get("RC_OPTIONS", 0)) & IGNORE_RECEIVER)
    radio_on, gcs_on = int(p["FS_THR_ENABLE"]) != 0, int(p["FS_GCS_ENABLE"]) != 0
    radio_wait, gcs_wait = float(p["RC_FS_TIMEOUT"]), float(p["FS_GCS_TIMEOUT"])
    radio_first = station and radio_on and gcs_on and radio_wait < gcs_wait

    settings: list[dict[str, Any]] = []
    status, verdict = (("out", "off") if not radio_on
                       else ("ok", "first") if radio_first else ("ok", "on"))  # fmt: skip
    settings.append({
        "param": "FS_THR_ENABLE", "value": p["FS_THR_ENABLE"],
        "action": _action(rules, RADIO, p["FS_THR_ENABLE"]),
        "wait_param": "RC_FS_TIMEOUT", "wait_s": radio_wait, "status": status, "verdict": verdict,
    })  # fmt: skip
    if not gcs_on:
        status, verdict = ("out", "off") if station else ("info", "off_pilot")
    else:
        status, verdict = "ok", "backstop" if radio_first else "first" if station else "on"
    settings.append({
        "param": "FS_GCS_ENABLE", "value": p["FS_GCS_ENABLE"],
        "action": _action(rules, GCS, p["FS_GCS_ENABLE"]),
        "wait_param": "FS_GCS_TIMEOUT", "wait_s": gcs_wait, "status": status, "verdict": verdict,
    })  # fmt: skip
    if gcs_on:
        row: dict[str, Any] = {"param": "FS_GCS_TIMEOUT", "value": gcs_wait}
        longest = None if uplink is None else uplink.get("longest_s")
        if longest is None or gcs_wait <= 0:
            row |= {"status": "info", "verdict": "unmeasured"}
        else:
            zone = reg.metrics["link.gcs_silence_share"].zone(longest / gcs_wait)
            row |= {"longest_s": longest, "margin_s": round(gcs_wait - longest, 2)}
            if zone == "out":  # it reached the wait: the failsafe analysis judges the loss
                row |= {"status": "info", "verdict": "reached"}
            else:
                row |= {"status": "caution" if zone == "caution" else "ok", "verdict": "margin"}
        settings.append(row)
    bits = int(p.get("FS_OPTIONS", 0))
    settings.append({
        "param": "FS_OPTIONS", "value": float(bits), "status": "info", "verdict": "options",
        "options": [name for name, bit in rules["options"].items() if bits >> int(bit) & 1],
    })  # fmt: skip
    if station and "RC_OVERRIDE_TIME" in p:
        default = flight.defaults.get("RC_OVERRIDE_TIME")
        settings.append({
            "param": "RC_OVERRIDE_TIME", "value": p["RC_OVERRIDE_TIME"], "default": default,
            "status": "info",
            "verdict": "value" if default is None
            else "default" if p["RC_OVERRIDE_TIME"] == default else "changed",
        })  # fmt: skip

    plays = (
        [("link_lost", True, True, False)] if station
        else [("radio_lost", True, False, False), ("telemetry_lost", False, True, True)]
    )  # fmt: skip
    words: Mapping[str, str] = doc["link"]["mode_words"]  # as the card names the modes
    losses: list[dict[str, Any]] = []
    for key, control, telemetry, pilot in plays:
        lines: dict[str, dict[str, Any]] = {}
        left: list[str] = []
        for mode in STARTS:
            start = State(mode, pilot=pilot)
            steps = play(rules, p, start, control, telemetry)
            lines[mode] = sequence(c, rules, start, steps)
            if alone(c, rules, steps[-1].state if steps else start):
                left.append(mode)
        losses.append({
            "id": key, "title": c.phrase(key)["text"],
            "lines": [line["text"] for line in by_mode(c, lines)],
            "alone": left, "alone_names": [str(words.get(m, m)) for m in left],
            "status": "out" if left else "ok",
        })  # fmt: skip
    return {"control": "station" if station else "receiver", "settings": settings,
            "losses": losses}  # fmt: skip


def links(flight: FlightData, pack: Pack, span: tuple[float, float] | None = None
          ) -> dict[str, Any]:  # fmt: skip
    """The Links view of one flight. `span` is not used: the view is the whole flight."""
    del span
    reg = Registry.load(pack)
    rules: Mapping[str, Any] = reg.rules["links"]
    clock = _Clock(flight)
    station = bool(int(_param(flight, "RC_OPTIONS", 0)) & IGNORE_RECEIVER)
    uplink = _uplink(flight, clock)
    radio = flight.answers.get("telemetry_radio")
    return {
        "flight_id": flight.id,
        "armed_s": clock.armed_s,
        "window_s": list(clock.window),
        "simulated": flight.simulated,
        "control": "station" if station else "receiver",
        "radio_kind": radio if isinstance(radio, str) else None,
        "tlogs": len(flight.tlogs),
        "tlogs_missing": sum(1 for s in flight.tlogs if s.get("status") == "missing"),
        "modes": _modes(flight, pack, clock),
        "downlink": _downlink(flight, rules, clock),
        "uplink": uplink,
        "joystick": _joystick(flight, station, clock),
        "radio": _radio(flight, reg, rules, clock),
        "receiver": _receiver(flight, station, clock),
        "failsafes": failsafes(flight, reg, pack, uplink),
    }
