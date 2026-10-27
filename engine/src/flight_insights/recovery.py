"""The recovery card per airframe (spec: Flying without GNSS, "Recommendations and
recovery"; Propulsion, "Motor-out margin"): what the aircraft will do when GNSS is lost
or spoofed with its current settings, how long it holds and how far it can go, and the
operator's steps; and the same when it loses its control link (recovery_link.py) or a
motor.

It is built on this laptop from:
- the airframe's latest parameters and setup answers, through the readiness review
  (analyzers/no_gnss_readiness.py);
- its measured outages and drills over its flights' latest analyses (the shortest hold,
  the longest detection and the worst drift a minute), kept apart by the source set they
  were flown on, so a hold measured on optical flow is never claimed for the aircraft on
  its own;
- its link failsafes and losses over the same analyses, and its failsafe settings played
  through ArduCopter's own failsafes (recovery_link.py);
- the motor-out margin of its latest flight that measured it on its current frame
  (analyzers/motor_out.py), turned into kilograms by that flight's weight: weighed at
  import, or estimated from the airframe's weighed flights (payload bands);
- the wording in knowledge/recovery/copter.json, in English and Ukrainian.

The card holds times, modes, switches, weights and how far the estimate drifts; never a
position. It is printed, never sent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any, cast

from flight_insights import recovery_link
from flight_insights.analyzers.flight import FlightData, FlightError
from flight_insights.analyzers.model import Result
from flight_insights.analyzers.motor_out import motor_frame
from flight_insights.analyzers.no_gnss_readiness import NO_GNSS_READINESS, channels
from flight_insights.analyzers.registry import LANGUAGES, Registry
from flight_insights.knowledge import Pack
from flight_insights.recommend.plan import render
from flight_insights.store.cache import ColumnCache
from flight_insights.store.db import Store
from flight_insights.trends import analysed_flights, flight_order

READINESS = "no_gnss:readiness:"
OUTAGE = "no_gnss:outage:"
MOTOR_OUT = "motor_out:hover"
NO_THRUST_LOSS_CHECK, GRIPPER_ON_THRUST_LOSS = 1, 4  # FLIGHT_OPTIONS bits (Copter.h)
PARACHUTE_RELEASE = (22, 23)  # aux functions: release, and the 3-position switch


class RecoveryError(ValueError):
    """No card can be made: an unknown airframe, or none of its flights has parameters."""


@dataclass(frozen=True)
class Measured:
    """Outages and drills of one kind, over the airframe's flights' latest analyses: flown
    on the GNSS source set (the aircraft on its own), or on another source set."""

    outages: int = 0  # outages and drills
    hold_s: float | None = None  # the shortest time it kept a position it could fly on
    detection_s: float | None = None  # the longest time to stop using GNSS
    drift_m_per_min: float | None = None  # the worst


@dataclass(frozen=True)
class MotorLoss:
    """Losing one motor on the airframe's current frame: whether its class can fly on the
    rest at all, and what its latest flight that measured it found."""

    frame: str  # "OCTA/X"
    can_lose_one: bool  # not a quad
    hold_yaw: bool  # the class's target holds the heading too
    measured: bool = False
    target_pct: float | None = None
    needed_pct: float | None = None  # the busiest remaining motor, at that flight's weight
    ratio: float | None = None  # the heaviest take-off within the target over that weight
    flight_kg: float | None = None  # that flight's weight
    weighed: bool = False  # on a scale, rather than estimated from the weighed flights


@dataclass
class Card:
    """The card's wording, and each phrase filled in both languages."""

    doc: Mapping[str, Any]

    def phrase(self, key: str, **params: Any) -> dict[str, Any]:
        texts: Mapping[str, Mapping[str, str]] = self.doc["texts"]
        sensors: Mapping[str, Mapping[str, str]] = self.doc["sensors"]
        out: dict[str, str] = {}
        for lang in LANGUAGES:
            # a phrase inside a phrase, and sensor names, in the reader's language
            local: dict[str, Any] = {
                k: cast(Mapping[str, str], v)[lang] if isinstance(v, dict) else v
                for k, v in params.items()
            }
            if "sensors" in params:
                local["sensors"] = ", ".join(
                    sensors.get(s, {}).get(lang, s) for s in params["sensors"]
                )
            out[lang] = render(texts[key][lang], local)
        return {"key": key, "params": dict(params), "text": out}


def _areas(readiness: Result) -> dict[str, Mapping[str, Any]]:
    return {
        e.id[len(READINESS) :]: e.values for e in readiness.evidence if e.id.startswith(READINESS)
    }


def build(
    readiness: Result, alone: Measured, on_set: Measured, params: Mapping[str, float],
    doc: Mapping[str, Any], tolerance_m: float, airframe: str, param_set: str | None,
    motor: MotorLoss | None = None, hover: tuple[float, float] = (10.0, 3.0),
    link: recovery_link.LinkMeasured | None = None,
    failsafes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The card from the readiness review's areas, the measured outages (on its own, and
    on another source set), the parameters, the motor-out margin and the link failsafes
    measured; the same inputs always give the same card. What was measured on one source
    set is never stated for the other. `hover` is the hover a motor-out measurement
    needs: seconds and height. The link sections need `failsafes`, the failsafe rules."""
    c = Card(doc)
    areas = _areas(readiness)
    position, switching = areas.get("position", {}), areas.get("switching", {})
    heading, fallback = areas.get("heading", {}), areas.get("pilot_fallback", {})
    wind = areas.get("wind", {})
    modes: Sequence[str] = fallback.get("modes", [])
    mode = "AltHold" if "AltHold" in modes else modes[0] if modes else None  # holds height
    station = bool(fallback.get("ground_station_control"))
    sets: Sequence[int] = position.get("sets", [])
    usable_sets: Sequence[int] = position.get("usable_sets", [])
    usable = bool(usable_sets)
    target = usable_sets[0] if usable_sets else sets[0] if sets else None
    sensors: list[str] = list(position.get("sensors", []))
    switch: Sequence[int] = switching.get("switch_channels", [])
    drill: Sequence[int] = fallback.get("drill_switch_channels", [])
    ekf_action = int(params.get("FS_EKF_ACTION", 1))
    ekf_on = ekf_action != 0 and params.get("FS_EKF_THRESH", 0.8) != 0

    # --- what the aircraft will do -------------------------------------------------------
    will: list[dict[str, Any]] = []
    if usable and target == 1:
        will.append(c.phrase("will.keeps_set", sensors=sensors, set=1))
    elif wind.get("drag_fusion") and params.get("FS_DR_ENABLE", 0):
        actions: Mapping[str, str] = doc["dead_reckoning_actions"]
        will.append(c.phrase(
            "will.dead_reckon", dr_action=actions.get(str(int(params["FS_DR_ENABLE"])), "?"),
            dr_timeout=params.get("FS_DR_TIMEOUT", 30.0),
        ))  # fmt: skip
    elif ekf_on:
        will.append(c.phrase("will.ekf", ekf_action=c.phrase(f"ekf.{ekf_action}")["text"]))
    else:
        will.append(c.phrase("will.ekf_off"))

    facts: list[dict[str, Any]] = []
    if alone.detection_s is not None:
        facts.append(c.phrase("fact.detection", detection_s=alone.detection_s))
    if alone.hold_s is not None:
        facts.append(c.phrase("fact.hold", hold_s=alone.hold_s, outages=alone.outages))
    else:
        facts.append(c.phrase("fact.hold_unmeasured"))
    if alone.drift_m_per_min:
        facts.append(c.phrase("fact.range", minutes=round(tolerance_m / alone.drift_m_per_min, 1),
                              tolerance_m=tolerance_m, rate=alone.drift_m_per_min))  # fmt: skip
    if usable and target != 1 and on_set.hold_s is not None:
        facts.append(c.phrase("fact.hold_set", set=target, hold_s=on_set.hold_s,
                              outages=on_set.outages))  # fmt: skip
        if on_set.drift_m_per_min:
            facts.append(c.phrase(
                "fact.range_set", set=target, tolerance_m=tolerance_m,
                minutes=round(tolerance_m / on_set.drift_m_per_min, 1),
                rate=on_set.drift_m_per_min,
            ))  # fmt: skip
    if not sets:
        facts.append(c.phrase("fact.no_fallback_set"))

    # --- the operator's steps when GNSS is lost --------------------------------------------
    take_over = (c.phrase("step.mode", mode=mode) if mode
                 else c.phrase("step.mode_station") if station
                 else c.phrase("step.no_mode"))  # fmt: skip
    steps: list[dict[str, Any]] = []
    fly_on_set = usable and (target == 1 or bool(switch))  # a source set to fly home on
    if usable and target != 1:
        steps.append(c.phrase("step.switch_set", set=target, channel=switch[0]) if switch
                     else c.phrase("step.no_switch", set=target))  # fmt: skip
    if fly_on_set:
        steps.append(c.phrase("step.fly_home_set", sensors=sensors, mode=mode or "AltHold"))
    else:  # home by hand, on the heading
        steps.append(take_over)
        bad_heading = heading.get("status") == "gap"
        steps.append(c.phrase("step.no_heading" if bad_heading else "step.heading"))
        if not bad_heading:
            steps.append(c.phrase("step.fly_home"))
    steps.append(c.phrase("step.land"))

    # --- suspected spoofing: reject GNSS first ---------------------------------------------
    spoof: list[dict[str, Any]] = [
        c.phrase("spoof.mode_first", mode=mode) if mode
        else c.phrase("spoof.mode_station") if station
        else c.phrase("step.no_mode")
    ]  # fmt: skip
    if drill:
        spoof.append(c.phrase("spoof.reject_switch", channel=drill[0]))
    elif usable and target != 1 and switch:
        spoof.append(c.phrase("spoof.reject_set", set=target, channel=switch[0]))
    else:
        spoof.append(c.phrase("spoof.no_reject"))
    if (drill or (usable and switch)) and ekf_on and ekf_action == 3:
        spoof.append(c.phrase("spoof.ekf_lands"))
    spoof.append(c.phrase("spoof.then"))

    numbers = {"alone": asdict(alone), "on_set": asdict(on_set), "tolerance_m": tolerance_m}
    sections = [
        {"id": "gnss_lost", "title": c.phrase("gnss_lost")["text"], "will": will,
         "facts": facts, "steps": steps},
        {"id": "spoofing", "title": c.phrase("spoofing")["text"], "will": [], "facts": [],
         "steps": spoof},
    ]  # fmt: skip
    if failsafes is not None:
        sections += recovery_link.sections(
            c, failsafes, params, link or recovery_link.LinkMeasured(),
            keeps_position=usable and target == 1, dead_reckons=bool(wind.get("drag_fusion")),
        )  # fmt: skip
    if motor is not None:
        sections.append(_motor_section(c, motor, params, hover))
    return {
        "airframe": airframe,
        "param_set": param_set,
        "title": c.phrase("card_title", airframe=airframe)["text"],
        "valid": c.phrase("card_valid", param_set=param_set)["text"] if param_set else None,
        "home_heading": c.phrase("home_heading")["text"],
        "will_label": c.phrase("will_label")["text"],
        "fallback_mode": mode or ("AltHold" if station else None),
        "measured": numbers,
        "sections": sections,
        "gaps": [f.code for f in readiness.findings],
        "footer": c.phrase("card_footer")["text"],
    }


def _motor_section(
    c: Card, m: MotorLoss, params: Mapping[str, float], hover: tuple[float, float]
) -> dict[str, Any]:
    """Losing a motor: what the firmware does (Copter crash_check.cpp, 4.7.0) and what the
    measured margin says the aircraft can do at the weight it last flew."""
    will: list[dict[str, Any]] = []
    down = not m.can_lose_one or (m.measured and m.ratio is None)
    if not m.can_lose_one:
        will.append(c.phrase("will.motor_quad"))
    elif m.measured and m.ratio is None:
        will.append(c.phrase("will.motor_no_balance"))
    elif m.measured and m.ratio is not None and m.needed_pct is not None:
        if m.needed_pct > 100.0:
            will.append(c.phrase("will.motor_sink", needed=m.needed_pct))
        elif m.ratio < 1.0:
            will.append(c.phrase("will.motor_short", needed=m.needed_pct, target=m.target_pct))
        else:
            will.append(c.phrase("will.motor_hold" if m.hold_yaw else "will.motor_hold_level"))
    options = int(params.get("FLIGHT_OPTIONS", 0))
    if m.can_lose_one and not options & NO_THRUST_LOSS_CHECK:
        will.append(c.phrase("will.motor_message"))
        if options & GRIPPER_ON_THRUST_LOSS and params.get("GRIP_ENABLE", 0):
            will.append(c.phrase("will.motor_gripper"))

    facts: list[dict[str, Any]] = []
    if m.can_lose_one and not m.measured:
        facts.append(c.phrase("fact.motor_unmeasured", hover_s=hover[0], height_m=hover[1]))
    elif m.ratio is not None and m.flight_kg:
        kg = round(m.flight_kg * m.ratio, 1)
        facts.append(
            c.phrase("fact.motor_kg", target=m.target_pct, kg=kg,
                     takeoff_kg=round(m.flight_kg, 1)) if m.weighed
            else c.phrase("fact.motor_kg_estimated", target=m.target_pct, kg=kg,
                          flight_kg=round(m.flight_kg, 1))
        )  # fmt: skip
    elif m.ratio is not None:
        facts.append(c.phrase("fact.motor_ratio", target=m.target_pct, ratio=round(m.ratio, 2)))

    steps: list[dict[str, Any]] = []
    if down:
        steps.append(c.phrase("step.motor_clear"))
        if params.get("CHUTE_ENABLED", 0):
            chute = channels(params, *PARACHUTE_RELEASE)
            steps.append(c.phrase("step.motor_chute_switch", channel=chute[0]) if chute
                         else c.phrase("step.motor_chute_station"))  # fmt: skip
    else:
        steps.append(c.phrase("step.motor_level"))
        if not m.hold_yaw:
            steps.append(c.phrase("step.motor_yaw"))
        steps += [c.phrase("step.motor_descend"), c.phrase("step.motor_land")]
    return {"id": "motor_lost", "title": c.phrase("motor_lost")["text"], "will": will,
            "facts": facts, "steps": steps}  # fmt: skip


def card_text(card: Mapping[str, Any], lang: str = "en") -> str:
    """The card as plain text, to print or keep with the aircraft."""
    lines = [card["title"][lang]]
    if card["valid"]:
        lines.append(card["valid"][lang])
    lines += [card["home_heading"][lang], ""]
    for s in card["sections"]:
        lines.append(s["title"][lang])
        lines += [f"- {card['will_label'][lang]} {p['text'][lang]}" for p in s["will"]]
        lines += [f"- {p['text'][lang]}" for p in s["facts"]]
        lines += [f"{i}. {p['text'][lang]}" for i, p in enumerate(s["steps"], start=1)]
        lines.append("")
    lines.append(card["footer"][lang])
    return "\n".join(lines) + "\n"


# --- from the store --------------------------------------------------------------------------


def _group(outages: Sequence[Mapping[str, Any]]) -> Measured:
    kept = [float(o["kept_s"]) for o in outages if o.get("kept_s") is not None]
    detection = [float(o["detection_s"]) for o in outages if o.get("detection_s") is not None]
    drift = [float(o["drift"]) for o in outages if o.get("drift") is not None]
    return Measured(
        outages=len(outages),
        hold_s=min(kept) if kept else None,
        detection_s=max(detection) if detection else None,
        drift_m_per_min=max(drift) if drift else None,
    )


def measured(store: Store, airframe_id: str) -> tuple[Measured, Measured]:
    """The airframe's outages and drills over its flights' latest analyses: those it met
    on the GNSS source set (on its own), and those on another source set."""
    alone: list[dict[str, Any]] = []
    on_set: list[dict[str, Any]] = []
    for (flight_id,) in store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ?", (airframe_id,)
    ).fetchall():
        run = store.conn.execute(
            "SELECT evidence_json FROM analysis_run WHERE flight_id = ? "
            "ORDER BY created_hlc DESC LIMIT 1",
            (flight_id,),
        ).fetchone()
        if run is None:
            continue
        evidence = {e["id"]: e.get("values", {}) for e in json.loads(run[0])}
        for eid, v in evidence.items():
            if not eid.startswith(OUTAGE) or eid.endswith(":drift"):
                continue
            o = dict(v) | {"drift": evidence.get(f"{eid}:drift", {}).get("drift_m_per_min")}
            other = v.get("source_set") not in (None, 1) or v.get("using_gnss_at_start") is False
            (on_set if other else alone).append(o)
    return _group(alone), _group(on_set)


def motor_loss(
    store: Store, reg: Registry, flight: FlightData, airframe_id: str
) -> MotorLoss | None:
    """Losing a motor on the frame `flight`'s parameters set, measured on the airframe's
    latest flight that measured it on that frame; None for a frame the matrix mixer does
    not set up (a helicopter, or a frame of its own)."""
    if flight.vehicle != "copter":
        return None
    found = motor_frame(flight, reg)
    if isinstance(found, str):
        return None
    name = found[1]
    target: Mapping[str, Any] = reg.rules["motor_out"]["classes"].get(
        str(int(flight.params["FRAME_CLASS"])), {}
    )
    loss = MotorLoss(name, bool(target.get("max_output")), bool(target.get("hold_yaw")))
    if not loss.can_lose_one:
        return loss
    flights, bands = analysed_flights(store, reg, airframe_id)
    for f in reversed(flights):
        row = store.conn.execute(
            "SELECT evidence_json FROM analysis_run WHERE id = ?", (f.run_id,)
        ).fetchone()
        v = next((e.get("values", {}) for e in json.loads(row[0]) if e["id"] == MOTOR_OUT), None)
        if v is None or v.get("frame") != name:  # measured on another frame
            continue
        kg = f.takeoff_kg or bands.kg(f.thrust)
        return replace(
            loss, measured=True, target_pct=v.get("target_pct"),
            needed_pct=v.get("worst_output_pct"), ratio=v.get("weight_ratio"),
            flight_kg=kg, weighed=f.takeoff_kg is not None,
        )  # fmt: skip
    return loss


def _latest(store: Store, cache: ColumnCache, pack: Pack, airframe_id: str) -> FlightData:
    cur = store.conn.execute(
        "SELECT id, boot_utc_us, arm_us, created_hlc FROM flight "
        "WHERE airframe_id = ? AND param_snapshot_id IS NOT NULL",
        (airframe_id,),
    )
    rows = [{d[0]: v for d, v in zip(cur.description, r, strict=True)} for r in cur.fetchall()]
    for row in sorted(rows, key=flight_order, reverse=True):
        try:
            return FlightData.load(store, cache, pack, row["id"])
        except FlightError:
            continue
    raise RecoveryError("none of the airframe's flights has its parameters")


def card(
    store: Store, cache: ColumnCache, pack: Pack, reg: Registry, airframe_id: str
) -> dict[str, Any]:
    """The airframe's card, from its latest flight's parameters, all its outages and its
    latest motor-out margin."""
    row = store.get("airframe", airframe_id)
    if row is None:
        raise RecoveryError("no such airframe")
    flight = _latest(store, cache, pack, airframe_id)
    readiness = NO_GNSS_READINESS.run(flight, reg)
    if readiness.skipped:
        raise RecoveryError(f"no card: {readiness.skipped}")
    snapshot = store.conn.execute(
        "SELECT s.set_id FROM flight f JOIN param_snapshot s ON s.id = f.param_snapshot_id "
        "WHERE f.id = ?",
        (flight.id,),
    ).fetchone()
    hover: Mapping[str, Any] = reg.rules["payload"]
    out = build(
        readiness, *measured(store, airframe_id), flight.params, pack.json("recovery/copter.json"),
        float(reg.rules["no_gnss"]["landing_tolerance_m"]), str(row["name"]),
        str(snapshot[0]).split("-", 1)[-1][:8] if snapshot else None,  # "ps1-<hash>"
        motor_loss(store, reg, flight, airframe_id),
        (float(hover["min_hover_s"]), float(hover["min_height_m"])),
        recovery_link.measured(store, airframe_id), reg.rules["failsafes"],
    )  # fmt: skip
    out["flight"] = flight.id
    out["text"] = {lang: card_text(out, lang) for lang in LANGUAGES}
    return out
