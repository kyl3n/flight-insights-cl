"""The recovery card's link sections (spec: Flying without GNSS, "Recommendations and
recovery"): what the aircraft will do when it loses its control link, with GNSS and
without, and the operator's steps.

What it will do is played through ArduCopter 4.7.0's own failsafes, as rebuilt in
rules/failsafes.json for judging logged failsafes (analyzers/failsafes.py), from the
moment the link drops:

- **Which failsafes a loss sets off, and when.** A pilot's receiver that stops sending
  sets off the radio failsafe RC_FS_TIMEOUT later (radio.cpp read_radio). A joystick flown
  through the ground station (RC_OPTIONS bit 0 ignores the receiver) sends its sticks as
  RC overrides, which count as the radio's input (RC_Channels::read_input), so losing that
  link sets off the radio failsafe RC_FS_TIMEOUT later too, then the ground-station
  failsafe at FS_GCS_TIMEOUT (events.cpp failsafe_gcs_check). Each acts on the mode the
  one before left it in.
- **What each does:** its setting, then the firmware's deviations (continue in Auto, in
  Guided, under pilot control, while landing), then its modes in turn: a mode that needs
  a position is refused without one, and the next is tried (set_mode_RTL_or_land_with_pause).
- **GNSS lost too.** Without a source set that keeps a position on its own, the aircraft
  loses its position: the dead-reckoning failsafe first where drag fusion lets it act,
  then the EKF failsafe (ekf_check.cpp failsafe_ekf_event: AltHold becomes Land while the
  radio failsafe is in force). Which goes first is not known, so both orders are played,
  from each mode it may be flown in, and every end they reach is named.
- **Home on dead reckoning.** Dead reckoning keeps a position the firmware flies on until
  FS_DR_TIMEOUT, whether or not FS_DR_ENABLE acts (system.cpp ekf_has_relative_position).
  It gets home on that position when FS_DR_TIMEOUT outlasts what the way home needs
  (`Return`): the trip from the farthest the airframe has flown (the RTL climb, then the
  way back) where the failsafe sends it home at once; that trip after its longest flight
  where it is left in its mode with GNSS lost first, for the link may go any time later.
  The position drifts, faster the longer it flies, since in 4.7.0 nothing corrects it
  (drag fusion corrects only the wind, which is held fixed while dead reckoning:
  AP_NavEKF3_AirDataFusion.cpp, AP_NavEKF3_core.cpp). Otherwise the timeout comes first,
  and the EKF failsafe as above.

The card says what was measured too: how the airframe's logged link failsafes went, losses
no failsafe answered, and the longest the ground station went unheard without one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from flight_insights.analyzers.failsafes import Context, expected
from flight_insights.analyzers.registry import LANGUAGES
from flight_insights.store.db import Store

if TYPE_CHECKING:
    from flight_insights.recovery import Card

RADIO, GCS, EKF, DEAD_RECKONING = (
    "FAILSAFE_RADIO", "FAILSAFE_GCS", "FAILSAFE_EKFINAV", "FAILSAFE_DEADRECKON",
)  # fmt: skip
IGNORE_RECEIVER = 1 << 0  # RC_OPTIONS: flown from the ground station
# the modes it may be flown in as it drops
STARTS = ("LOITER", "POSHOLD", "ALT_HOLD", "GUIDED", "AUTO")
VERDICTS = ("as_configured", "fell_back", "unexpected")  # failsafe.<verdict> findings


@dataclass(frozen=True)
class State:
    mode: str  # as rules/failsafes.json names it; "disarm" once its motors stop
    position: bool = True  # a position it can fly on
    radio: bool = False  # the radio failsafe in force
    pilot: bool = False  # someone still flies it: the pilot's radio works
    dead_reckoning: bool = False  # its position is dead-reckoned: it drifts, nothing corrects it


@dataclass(frozen=True)
class Step:
    after_s: float | None  # seconds after the loss; None where it depends on GNSS
    failsafe: str
    state: State


@dataclass(frozen=True)
class Kind:
    """One kind of link failsafe over the airframe's flights' latest analyses."""

    verdicts: dict[str, int] = field(default_factory=dict[str, int])  # as configured, ...
    unanswered: int = 0  # losses no failsafe answered
    slowest_s: float | None = None  # the longest from the loss to the failsafe
    wait_s: float | None = None  # its setting's wait on that flight


@dataclass(frozen=True)
class LinkMeasured:
    radio: Kind = Kind()
    gcs: Kind = Kind()
    gcs_unheard_s: float | None = None  # the longest silence short of the failsafe's wait
    gcs_timeout_s: float | None = None


def _definite(value: Any, otherwise: bool) -> bool:
    """A mode's trait as it is while flying a leg: Auto and RTL are not landing yet."""
    return otherwise if value in (None, "maybe") else bool(value)


def needs_position(rules: Mapping[str, Any], mode: str) -> bool:
    return _definite(rules["modes"].get(mode, {}).get("position"), True)


def _enter(rules: Mapping[str, Any], state: State, modes: Sequence[str | None]) -> State:
    """The first of `modes` the vehicle can be in: asking for its own mode changes nothing
    (mode.cpp), and one that needs a position is refused without one. None: carry on."""
    for mode in modes:
        if mode is None or mode == state.mode:
            return state
        if mode == "disarm":
            return replace(state, mode="disarm")
        if state.position or not needs_position(rules, mode):
            return replace(state, mode=mode)
    return state


def act(rules: Mapping[str, Any], params: Mapping[str, float], state: State, kind: str) -> State:
    """What one failsafe does to the vehicle in `state`, by the firmware's own order."""
    if state.mode == "disarm":
        return state
    if kind == DEAD_RECKONING:  # in modes that need a position, no deviations
        if not needs_position(rules, state.mode):
            return state
        action = rules["failsafes"]["FAILSAFE_BATT"]["actions"].get(
            str(int(params.get("FS_DR_ENABLE", 0))), "NONE"
        )  # the dead-reckoning failsafe takes the same FailsafeAction values
        chain = rules["chains"][action]
        return _enter(rules, state, ["disarm"] if chain == "disarm" else chain or [None])
    spec: Mapping[str, Any] = rules["modes"].get(state.mode, {})
    ctx = Context(
        mode=state.mode, ground=False, landing=_definite(spec.get("landing"), False),
        guided=_definite(spec.get("guided"), False), position=needs_position(rules, state.mode),
        battery_lands=False, radio_active=state.radio,
    )  # fmt: skip
    rule: Mapping[str, Any] = rules["failsafes"][kind]
    configured = [] if kind == EKF else [  # the EKF failsafe reads FS_EKF_ACTION itself
        str(rule["actions"].get(str(int(params[rule["param"]])), rule["otherwise"]))
    ]  # fmt: skip
    outcomes = expected(kind, rules, params, ctx, configured)
    after = _enter(rules, state, [o.asked if o.what != "disarm" else "disarm" for o in outcomes])
    return replace(after, radio=state.radio or kind == RADIO)


def _link_events(params: Mapping[str, float], control: bool, telemetry: bool
                 ) -> list[tuple[float, str]]:  # fmt: skip
    """The failsafes losing the link sets off, at their waits."""
    out: list[tuple[float, str]] = []
    if control and int(params["FS_THR_ENABLE"]):
        out.append((float(params["RC_FS_TIMEOUT"]), RADIO))
    if telemetry and int(params["FS_GCS_ENABLE"]):
        out.append((float(params["FS_GCS_TIMEOUT"]), GCS))
    return sorted(out)


@dataclass(frozen=True)
class Return:
    """What a way home on dead reckoning needs of FS_DR_TIMEOUT: the trip home from the
    farthest the airframe has flown, and its longest flight (with GNSS lost first, the link
    may go any time later in the flight, and the trip starts then)."""

    trip_s: float
    flight_s: float


def dead_reckoned_return(
    params: Mapping[str, float], defaults: Mapping[str, float], farthest_m: float | None,
    flight_s: float | None,
) -> Return | None:  # fmt: skip
    """The trip RTL flies home from the farthest the airframe has flown: the climb to
    RTL_ALT_M at WP_SPD_UP from the ground (the most it can take), then the way back at
    RTL_SPEED_MS, or WP_SPD where that is 0 (mode_rtl.cpp); with its longest flight. None
    when no flight measured them. A setting the parameters do not hold is the firmware's
    default."""
    if farthest_m is None or flight_s is None:
        return None
    p: Mapping[str, float] = dict(defaults) | dict(params)
    speed = float(p["RTL_SPEED_MS"]) or float(p["WP_SPD"])
    trip = float(p["RTL_ALT_M"]) / float(p["WP_SPD_UP"]) + farthest_m / speed
    return Return(trip, flight_s)


def _gnss_lost(
    rules: Mapping[str, Any], params: Mapping[str, float], state: State, dead_reckons: bool,
    ekf_on: bool, back: Return | None = None, link_later: bool = False,
) -> list[Step]:  # fmt: skip
    """GNSS lost with no source set keeping a position: dead reckoning where drag fusion
    lets it, its failsafe acting where FS_DR_ENABLE says, and the position kept while
    FS_DR_TIMEOUT outlasts what the way home needs (`back`): the trip, from home-going
    modes; else, with the link to go later (`link_later`), the trip after its longest
    flight. Otherwise the position is given up at the timeout, and the EKF failsafe acts."""
    steps: list[Step] = []
    if dead_reckons:
        state = replace(state, dead_reckoning=True)
        if int(params.get("FS_DR_ENABLE", 0)) > 0:
            state = act(rules, params, state, DEAD_RECKONING)
        steps.append(Step(None, DEAD_RECKONING, state))
        if back is not None:
            need = (back.trip_s if state.mode in HOME
                    else back.flight_s + back.trip_s if link_later else None)  # fmt: skip
            if need is not None and float(params.get("FS_DR_TIMEOUT", 30.0)) >= need:
                return steps
    state = replace(state, position=False, dead_reckoning=False)
    steps.append(Step(None, EKF, act(rules, params, state, EKF) if ekf_on else state))
    return steps


def play(rules: Mapping[str, Any], params: Mapping[str, float], start: State, control: bool,
         telemetry: bool) -> list[Step]:  # fmt: skip
    """The link lost in `start`: each failsafe it sets off, with the state it leaves."""
    state, steps = start, list[Step]()
    for at, kind in _link_events(params, control, telemetry):
        state = act(rules, params, state, kind)
        steps.append(Step(at, kind, state))
    return steps


def both_lost(
    rules: Mapping[str, Any], params: Mapping[str, float], start: State, control: bool,
    telemetry: bool, dead_reckons: bool, ekf_on: bool, back: Return | None = None,
) -> list[State]:  # fmt: skip
    """The link and GNSS lost, in either order: where each order ends."""

    def link(state: State) -> State:
        for _, kind in _link_events(params, control, telemetry):
            state = act(rules, params, state, kind)
        return state

    def gnss(state: State, link_later: bool) -> State:
        steps = _gnss_lost(rules, params, state, dead_reckons, ekf_on, back, link_later)
        return steps[-1].state

    return list(dict.fromkeys((gnss(link(start), False), link(gnss(start, True)))))


# --- wording ------------------------------------------------------------------------------


def _words(c: Card, mode: str) -> str:
    return str(c.doc["link"]["mode_words"].get(mode, mode))


def outcome(c: Card, rules: Mapping[str, Any], s: State, start: str | None = None
            ) -> dict[str, Any]:  # fmt: skip
    """What the vehicle in state `s` does, as words. Still in its `start` mode, the line
    that names the mode says which; any other mode is named here."""
    m = s.mode
    if m == "disarm":
        return c.phrase("out.disarm")
    if m == "LAND":
        return c.phrase("out.land" if s.position else "out.land_drift")
    if m in HOME and s.position and s.dead_reckoning:
        return c.phrase(f"out.{m.lower()}_dead_reckoning")
    if m in ("RTL", "SMART_RTL", "AUTO_RTL", "BRAKE", "AUTO", "GUIDED") and s.position:
        return c.phrase(f"out.{m.lower()}")
    key = ("out.lost" if needs_position(rules, m) and not s.position  # the EKF failsafe off
           else "out.flown" if s.pilot
           else "out.hold_alone" if needs_position(rules, m)
           else "out.drift_alone")  # fmt: skip
    return c.phrase(key) if m == start else c.phrase(f"{key}_in", mode=_words(c, m))


ALONE = ("out.lost", "out.hold_alone", "out.drift_alone")  # no one flies it, or it is lost
HOME = ("RTL", "SMART_RTL")  # back to take-off: RTL, or SmartRTL back the way it came


def alone(c: Card, rules: Mapping[str, Any], s: State) -> bool:
    """Whether the vehicle in state `s` is left with no one flying it, or flies on with no
    position it can trust: what the card marks as an alert."""
    return str(outcome(c, rules, s)["key"]).removesuffix("_in") in ALONE


def gets_home(
    rules: Mapping[str, Any], params: Mapping[str, float], control: bool, telemetry: bool,
    keeps_position: bool, dead_reckons: bool, ekf_on: bool, back: Return | None = None,
) -> str | None:  # fmt: skip
    """The goal (spec: Flying without GNSS, "Recommendations and recovery"): with the link
    and GNSS both lost, whichever goes first and from every mode it may be flown in, it
    flies back to take-off. "position" when every way home keeps a position it can trust;
    "dead_reckoning" when some way home flies on dead reckoning, which drifts (the fleet
    owner counts it, 2026-10-27); None when it does not get home. A first source set that
    keeps a position leaves the link's failsafes to act as they would with GNSS."""
    by = "position"
    for m in STARTS:
        if keeps_position:
            played = play(rules, params, State(m), control, telemetry)
            ends = [played[-1].state if played else State(m)]
        else:
            ends = both_lost(rules, params, State(m), control, telemetry, dead_reckons, ekf_on,
                             back)  # fmt: skip
        if not all(e.mode in HOME and e.position for e in ends):
            return None
        if any(e.dead_reckoning for e in ends):
            by = "dead_reckoning"
    return by


def goal_met(
    rules: Mapping[str, Any], defaults: Mapping[str, float], params: Mapping[str, float],
    keeps_position: bool, dead_reckons: bool, flown: tuple[float | None, float | None] = (
        None, None),
) -> bool:  # fmt: skip
    """The goal as the card's link section plays it (`sections`): the one link a
    station-flown aircraft has, else the pilot's radio, lost with GNSS; met on a trusted
    position or on dead reckoning. `flown` is the farthest the airframe has flown and its
    longest flight (`flown`). A setting the parameters do not hold is the firmware's
    default (`defaults`, the card's link defaults)."""
    p: Mapping[str, float] = dict(defaults) | dict(params)
    station = bool(int(p.get("RC_OPTIONS", 0)) & IGNORE_RECEIVER)
    ekf_on = int(p.get("FS_EKF_ACTION", 1)) != 0 and p.get("FS_EKF_THRESH", 0.8) != 0
    back = dead_reckoned_return(p, defaults, *flown)
    return gets_home(rules, p, True, station, keeps_position, dead_reckons, ekf_on,
                     back) is not None  # fmt: skip


def _joined(c: Card, items: Sequence[Mapping[str, str]]) -> dict[str, str]:
    """Texts in the reader's language as "a, b or c"."""
    word: Mapping[str, str] = c.doc["texts"]["word.or"]
    out: dict[str, str] = {}
    for lang in word:
        parts = [i[lang] for i in items]
        head, last = ", ".join(parts[:-1]), parts[-1]
        out[lang] = f"{head} {word[lang]} {last}" if head else last
    return out


def sequence(c: Card, rules: Mapping[str, Any], start: State, steps: Sequence[Step]
             ) -> dict[str, Any]:  # fmt: skip
    """The failsafes that change what it does, in turn; or that it carries on: with no
    failsafe on, or with one whose settings let it."""
    names: Mapping[str, Mapping[str, str]] = c.doc["link"]["failsafe_names"]
    said: dict[str, Any] | None = None
    last = outcome(c, rules, start, start.mode)
    for s in steps:
        now = outcome(c, rules, s.state, start.mode)
        if now["text"] == last["text"]:
            continue
        step = c.phrase("link.step", outcome=now["text"], t=s.after_s, failsafe=names[s.failsafe])
        said = step if said is None else c.phrase("link.then", a=said["text"], b=step["text"])
        last = now
    if said is not None:
        return said
    return c.phrase("link.let_on" if steps else "link.no_failsafe", outcome=last["text"])


def by_mode(c: Card, lines: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per different outcome, naming the modes it is for; one line for all."""
    groups: dict[str, list[str]] = {}
    for mode, line in lines.items():
        groups.setdefault(line["text"]["en"], []).append(mode)
    if len(groups) == 1:
        return [c.phrase("link.any_mode", sequence=next(iter(lines.values()))["text"])]
    out: list[dict[str, Any]] = []
    for modes in groups.values():
        words = _joined(c, [dict.fromkeys(LANGUAGES, _words(c, m)) for m in modes])
        out.append(c.phrase("link.in_modes", sequence=lines[modes[0]]["text"], modes=words))
    return out


# --- the sections -------------------------------------------------------------------------


def _facts(c: Card, kind: Kind, name: Mapping[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = sum(kind.verdicts.values())
    if total:
        out.append(c.phrase("fact.link_failsafes", n=total, failsafe=name,
                            **{v: kind.verdicts.get(v, 0) for v in VERDICTS}))  # fmt: skip
    if kind.slowest_s is not None and kind.wait_s is not None:
        out.append(c.phrase("fact.link_slowest", failsafe=name, waited_s=kind.slowest_s,
                            wait_s=kind.wait_s))  # fmt: skip
    if kind.unanswered:
        out.append(c.phrase("fact.link_unanswered", n=kind.unanswered, failsafe=name))
    return out


def sections(
    c: Card, rules: Mapping[str, Any], params: Mapping[str, float], measured: LinkMeasured,
    keeps_position: bool, dead_reckons: bool,
    flown: tuple[float | None, float | None] = (None, None),
) -> list[dict[str, Any]]:  # fmt: skip
    """The link sections: one for a ground station that flies it; for a pilot's radio, one
    for the radio and one for the telemetry link. A setting the log did not hold is the
    firmware's default. `flown`, the farthest the airframe has flown and its longest flight
    (`flown`), sets what a way home on dead reckoning needs of FS_DR_TIMEOUT."""
    doc: Mapping[str, Any] = c.doc["link"]
    p: Mapping[str, float] = dict(doc["defaults"]) | dict(params)
    back = dead_reckoned_return(p, doc["defaults"], *flown)
    names: Mapping[str, Mapping[str, str]] = doc["failsafe_names"]
    station = bool(int(p.get("RC_OPTIONS", 0)) & IGNORE_RECEIVER)
    ekf_on = int(p.get("FS_EKF_ACTION", 1)) != 0 and p.get("FS_EKF_THRESH", 0.8) != 0

    def lines(control: bool, telemetry: bool, pilot: bool) -> list[dict[str, Any]]:
        return by_mode(c, {
            m: sequence(c, rules, State(m, pilot=pilot),
                        play(rules, p, State(m, pilot=pilot), control, telemetry))
            for m in STARTS
        })  # fmt: skip

    def gnss_too(control: bool, telemetry: bool) -> dict[str, Any]:
        if keeps_position:  # source set 1 keeps a position: losing GNSS changes nothing
            return c.phrase("link.gnss_keeps")
        said: dict[str, dict[str, str]] = {}  # each different end once, in the order met
        for m in STARTS:
            for end in both_lost(rules, p, State(m), control, telemetry, dead_reckons, ekf_on,
                                 back):  # fmt: skip
                words = outcome(c, rules, end)["text"]
                said.setdefault(words["en"], words)
        key = "link.gnss_too" if len(said) == 1 else "link.gnss_too_depends"
        return c.phrase(key, outcomes=_joined(c, list(said.values())))

    def lands_or_stays(control: bool, telemetry: bool) -> bool:
        """Whether, flown in some mode, it ends up landing or staying where it is."""
        for m in STARTS:
            steps = play(rules, p, State(m), control, telemetry)
            end = steps[-1].state if steps else State(m)
            if end.mode not in ("RTL", "SMART_RTL", "AUTO_RTL", "AUTO", "GUIDED", "disarm"):
                return True
        return False

    def left_alone(control: bool, telemetry: bool, pilot: bool, gnss_too: bool) -> bool:
        """Whether, flown in some mode, it ends with no one flying it or lost; with GNSS
        lost too, in either order, where the section says what happens then."""
        for m in STARTS:
            start = State(m, pilot=pilot)
            played = play(rules, p, start, control, telemetry)
            ends = [played[-1].state if played else start]
            if gnss_too and not keeps_position:
                ends += both_lost(rules, p, State(m), control, telemetry, dead_reckons, ekf_on,
                                  back)  # fmt: skip
            if any(alone(c, rules, e) for e in ends):
                return True
        return False

    def home_dead_reckoned(control: bool, telemetry: bool) -> bool:
        """Whether, with GNSS lost too, some way home flies on dead reckoning."""
        return not keeps_position and any(
            e.dead_reckoning
            for m in STARTS
            for e in both_lost(rules, p, State(m), control, telemetry, dead_reckons, ekf_on, back)
        )

    def steps(control: bool, telemetry: bool, *first: str) -> list[dict[str, Any]]:
        out = [c.phrase(k) for k in first]
        if not _link_events(p, control, telemetry):
            out.append(c.phrase("step.link_no_failsafe"))
        if lands_or_stays(control, telemetry) or not keeps_position:
            out.append(c.phrase("step.link_clear"))
        if home_dead_reckoned(control, telemetry):
            out.append(c.phrase("step.dead_reckoned_home"))
        return out

    def section(key: str, will: list[dict[str, Any]], facts: list[dict[str, Any]],
                steps: list[dict[str, Any]], alert: bool) -> dict[str, Any]:  # fmt: skip
        return {"id": key, "title": c.phrase(key)["text"], "will": will,
                "facts": facts or [c.phrase("fact.link_unmeasured")], "steps": steps,
                "alert": alert}  # fmt: skip

    def goal(control: bool, telemetry: bool) -> dict[str, Any]:
        """Whether it gets home with this link and GNSS both lost, and the lines that say
        what it does: the link's failsafes from each mode, then with GNSS lost too."""
        by = gets_home(rules, p, control, telemetry, keeps_position, dead_reckons, ekf_on, back)
        return {
            "met": by is not None,
            "by": by,
            "trip_s": None if back is None else round(back.trip_s),
            "will": [*lines(control, telemetry, False), gnss_too(control, telemetry)],
        }

    radio_facts = _facts(c, measured.radio, names[RADIO])
    gcs_facts = [*_facts(c, measured.gcs, names[GCS]), *_silence(c, measured)]
    if station:
        return [section(
            "link_lost",
            [c.phrase("will.link_station"), *lines(True, True, False), gnss_too(True, True)],
            [*radio_facts, *gcs_facts],
            steps(True, True, "step.link_antennas", "step.link_back_station"),
            left_alone(True, True, False, gnss_too=True)
            or bool(measured.radio.unanswered or measured.gcs.unanswered),
        ) | {"goal": goal(True, True)}]  # fmt: skip
    receiver = (  # what sets the radio failsafe off, and what never does
        [c.phrase("will.radio_throttle", value=float(p["FS_THR_VALUE"])),
         c.phrase("will.radio_hold")]
        if int(p["FS_THR_ENABLE"]) else []
    )  # fmt: skip
    return [
        section("radio_lost", [*lines(True, False, False), *receiver, gnss_too(True, False)],
                radio_facts, steps(True, False, "step.radio_antennas", "step.link_back_switch"),
                left_alone(True, False, False, gnss_too=True) or measured.radio.unanswered > 0)
        | {"goal": goal(True, False)},
        section("telemetry_lost", lines(False, True, True), gcs_facts,
                # the pilot still flies it: a telemetry loss no failsafe answered is no alert
                [c.phrase("step.telemetry_fly")], left_alone(False, True, True, gnss_too=False)),
    ]  # fmt: skip


def _silence(c: Card, m: LinkMeasured) -> list[dict[str, Any]]:
    if m.gcs_unheard_s is None or m.gcs_timeout_s is None:
        return []
    return [c.phrase("fact.link_near_miss", seconds=m.gcs_unheard_s, timeout=m.gcs_timeout_s)]


# --- from the store -----------------------------------------------------------------------


def flown(store: Store, airframe_id: str) -> tuple[float | None, float | None]:
    """The farthest the airframe has flown from where it armed, and its longest flight,
    over its flights' latest analyses (the no-GNSS analyzer's `no_gnss:range`); None for
    each when none measured it."""
    far: list[float] = []
    armed: list[float] = []
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
        for e in json.loads(run[0]):
            v: Mapping[str, Any] = e.get("values", {}) if e["id"] == "no_gnss:range" else {}
            if v.get("farthest_m") is not None:
                far.append(float(v["farthest_m"]))
            if v.get("armed_s") is not None:
                armed.append(float(v["armed_s"]))
    return (max(far) if far else None), (max(armed) if armed else None)


def measured(store: Store, airframe_id: str) -> LinkMeasured:
    """The airframe's link failsafes, unanswered losses and longest ground-station silence
    short of its failsafe, over its flights' latest analyses."""
    verdicts: dict[str, dict[str, int]] = {RADIO: {}, GCS: {}}
    unanswered = {RADIO: 0, GCS: 0}
    slowest: dict[str, tuple[float, float]] = {}
    unheard: tuple[float, float] | None = None
    for (flight_id,) in store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ?", (airframe_id,)
    ).fetchall():
        run = store.conn.execute(
            "SELECT id, evidence_json FROM analysis_run WHERE flight_id = ? "
            "ORDER BY created_hlc DESC LIMIT 1",
            (flight_id,),
        ).fetchone()
        if run is None:
            continue
        evidence = {e["id"]: e.get("values", {}) for e in json.loads(run[1])}
        for code, cited in store.conn.execute(
            "SELECT code, evidence_json FROM finding WHERE run_id = ? AND code LIKE 'failsafe.%'",
            (run[0],),
        ).fetchall():
            for eid in json.loads(cited):
                v = evidence.get(eid, {})
                kind = v.get("failsafe")
                if kind not in verdicts:
                    continue
                verdict = code.split(".", 1)[1]
                if verdict in VERDICTS:
                    verdicts[kind][verdict] = verdicts[kind].get(verdict, 0) + 1
                    waited, wait = v.get("waited_s"), v.get("wait_s")
                    if (
                        waited is not None
                        and wait is not None
                        and (kind not in slowest or waited > slowest[kind][0])
                    ):
                        slowest[kind] = (float(waited), float(wait))
                elif verdict in ("missed", "off_during_loss"):
                    unanswered[kind] += 1
        silence = evidence.get("link:gcs_silence")
        if (
            silence
            and float(silence["longest_s"]) < float(silence["timeout_s"])
            and (unheard is None or float(silence["longest_s"]) > unheard[0])
        ):
            unheard = (float(silence["longest_s"]), float(silence["timeout_s"]))

    def summed(k: str) -> Kind:
        worst = slowest.get(k)
        return Kind(verdicts[k], unanswered[k], *(worst or (None, None)))

    return LinkMeasured(summed(RADIO), summed(GCS), *(unheard or (None, None)))
