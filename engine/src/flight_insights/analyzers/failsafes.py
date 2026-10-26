"""Whether each failsafe did what its settings call for (spec: Failsafe causes and RF
early warning, "Did the failsafe work?").

A failsafe's error (ERR, FAILSAFE_OCCURRED) is followed in the same loop by its action:
a mode change logged with the failsafe's reason (MODE.Rsn), a disarm logged with its
method (ARM.Method), or nothing. The action the settings call for is rebuilt from
ArduCopter's own source (rules/failsafes.json): the failsafe's setting, then the
firmware's deviations in order (disarm on the ground; land while landing with a battery
failsafe active, or with FS_OPTIONS 'continue if landing'; continue in Auto, Guided or
under pilot control where FS_OPTIONS says so), then the modes it tries in turn.

What the log cannot settle is left open rather than guessed: whether RTL or SmartRTL
was in its landing phase, whether Auto was flying a landing or a guided leg (read from
the mission command running, CMD, where logged), whether the throttle stick was at
zero, which battery stage fired. Every outcome the firmware could have chosen counts,
so a response is called unexpected only when no reading of the log allows it.

- **As configured** (info): the first mode of its chain, a disarm on the ground, or
  continuing where the settings say to. That a failsafe fired at all is the system
  health analyzer's finding.
- **Fell back** (caution): a later mode of the chain, because the first could not be
  entered (SmartRTL with no return path, RTL with no home position).
- **Unexpected** (out): nothing the settings and the log allow; a parameter changed
  in flight, a script or a firmware this model does not cover.

Only Copter's failsafes are modelled here (Plane and QuadPlane: Phase 3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, Finding, Result, Segment
from flight_insights.analyzers.registry import Registry

Array = np.ndarray[Any, Any]
Maybe = Literal[True, False, "maybe"]
Change = tuple[int, str, str]  # time, mode, reason

STARTED, RESOLVED = "FAILSAFE_OCCURRED", ("FAILSAFE_RESOLVED", "ERROR_RESOLVED")
NAV_LAST, NAV_SCRIPT_TIME, NAV_ATTITUDE_TIME = 95, 42702, 42703  # AP_Mission::is_nav_cmd
NAV_RTL, NAV_LAND = 20, (21, 85)  # MAV_CMD_NAV_RETURN_TO_LAUNCH, NAV_LAND, NAV_VTOL_LAND
NAV_GUIDED = (92, NAV_SCRIPT_TIME)  # Auto's in_guided_mode
THROTTLE_ZERO_S = 0.4  # THROTTLE_ZERO_DEBOUNCE_TIME_MS
BATTERY_HEALTH_S = 5.0  # AP_BattMonitor_Backend: no healthy reading for 5 s
LANDING_PRIORITY = ("TERMINATE", "LAND")  # _failsafe_priorities up to FAILSAFE_LAND_PRIORITY
STAGE_RANK = {"unhealthy": 0, "low": 1, "critical": 2}  # AP_BattMonitor::Failsafe, rising
MONITORS = ("BATT", *(f"BATT{n}" for n in "23456789ABCDEFG"))


@dataclass(frozen=True)
class Occurrence:
    kind: str  # its ERR subsystem: FAILSAFE_RADIO ...
    start_us: int
    end_us: int | None  # when it was logged resolved, if it was


@dataclass
class Context:
    """The vehicle's state as the failsafe fired, as far as the log says."""

    mode: str
    ground: Maybe
    landing: Maybe
    guided: Maybe
    position: Maybe  # whether the mode needs a position
    battery_lands: Maybe  # an active battery failsafe whose action was Land or Terminate
    radio_active: bool
    open: list[str] = field(default_factory=list[str])  # what the log left open, where it mattered
    read: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # message, field

    def ask(self, name: str) -> Maybe:
        """One of the state's values, as a decision reads it; one the log leaves open is
        noted, so a verdict's confidence counts only what it depended on."""
        value: Maybe = getattr(self, name)
        if value == "maybe":
            self.note(name)
        return value

    def note(self, *names: str) -> None:
        self.open += [n for n in names if n not in self.open]


@dataclass(frozen=True)
class Outcome:
    what: str  # "continue", "disarm" or a mode's name
    fallback_of: str | None = None  # the mode it was a fallback for, if it was one
    asked: str | None = None  # the mode the firmware asked for, though it may be in it


def _ints(table: pa.Table, name: str) -> Array:
    return np.asarray(table.column(name).to_numpy(zero_copy_only=False), np.int64)


def _both(a: Maybe, b: Maybe) -> Maybe:
    if a is False or b is False:
        return False
    return True if a is True and b is True else "maybe"


def occurrences(
    flight: FlightData, names: Mapping[str, Any], kinds: Sequence[str]
) -> list[Occurrence]:
    """Each failsafe of the given kinds, from its error to the one that resolved it. A
    second error before that (a battery's critical stage after its low one) is a failsafe
    of its own: the firmware acted on it again."""
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    if err is None:
        return []
    subsystems: Mapping[str, str] = names.get("error_subsystems", {})
    codes: Mapping[str, list[str]] = names.get("error_codes", {})
    t, sub, code = _ints(err, "TimeUS"), _ints(err, "Subsys"), _ints(err, "ECode")
    out: list[Occurrence] = []
    open_: dict[str, list[int]] = {}
    for k in np.argsort(t, kind="stable"):
        kind = subsystems.get(str(int(sub[k])), "")
        if kind not in kinds:
            continue
        meaning = codes.get(str(int(code[k])), [])
        if STARTED in meaning:
            open_.setdefault(kind, []).append(int(t[k]))
        elif any(r in meaning for r in RESOLVED):
            out += [Occurrence(kind, start, int(t[k])) for start in open_.pop(kind, [])]
    out += [Occurrence(kind, s, None) for kind, starts in open_.items() for s in starts]
    return sorted(out, key=lambda o: o.start_us)


def _modes(flight: FlightData, names: Mapping[str, Any]) -> list[Change]:
    """(time, mode, reason) of every mode change, in order."""
    table = flight.table("MODE")
    if table is None or table.num_rows == 0:
        return []
    modes: Mapping[str, str] = names["modes"].get("copter", {})
    reasons: Mapping[str, str] = names.get("mode_reasons", {})
    t, n = _ints(table, "TimeUS"), _ints(table, "ModeNum")
    has_reason = "Rsn" in table.column_names
    rsn = _ints(table, "Rsn") if has_reason else np.zeros(len(t), np.int64)
    return [
        (int(t[i]), modes.get(str(int(n[i])), f"MODE_{int(n[i])}"),
         reasons.get(str(int(rsn[i])), "UNKNOWN"))
        for i in np.argsort(t, kind="stable")
    ]  # fmt: skip


def _mode_at(changes: Sequence[Change], t: int, own_reason: str) -> str:
    """The mode as the failsafe fired. The firmware logs its error and its mode change in
    the same loop, often with one timestamp, so a change at that moment for this
    failsafe's own reason is its response, not the mode it fired in."""
    before = [m for at, m, why in changes if at < t or (at == t and why != own_reason)]
    return before[-1] if before else "UNKNOWN"


def _landed(flight: FlightData, names: Mapping[str, Any], t: int) -> Maybe:
    """Landed by the land detector's own events (EV): landed at arming until it says
    otherwise."""
    ev = flight.table("EV", ["TimeUS", "Id"])
    if ev is None:
        return "maybe"
    events: Mapping[str, str] = names.get("events", {})
    state: Maybe = True
    times, ids = _ints(ev, "TimeUS"), _ints(ev, "Id")
    for k in np.argsort(times, kind="stable"):
        if times[k] > t:
            break
        name = events.get(str(int(ids[k])), "")
        if name == "LAND_COMPLETE":
            state = True
        elif name == "NOT_LANDED":
            state = False
    return state


def _throttle_zero(flight: FlightData, t: int) -> Maybe:
    """Whether the firmware's throttle-zero flag was set: the pilot's throttle (CTUN.ThI)
    at zero for longer than the debounce time. Between two samples the stick's moment is
    unknown, so only a span that settles it either way counts. Air mode, which keeps the
    flag clear, is not logged."""
    ctun = flight.table("CTUN", ["TimeUS", "ThI"])
    if ctun is None or ctun.num_rows == 0:
        return "maybe"
    times = _ints(ctun, "TimeUS")
    thi = np.asarray(ctun.column("ThI").to_numpy(zero_copy_only=False), np.float64)
    up_to = times <= t
    times, thi = times[up_to], thi[up_to]
    if not len(times):
        return "maybe"
    debounce = THROTTLE_ZERO_S * 1e6
    moving = np.flatnonzero(thi > 0)
    if len(moving) and t - times[moving[-1]] <= debounce:
        return False  # off zero within the debounce time
    first_zero = times[moving[-1] + 1] if len(moving) else times[0]
    if len(moving) and moving[-1] + 1 >= len(times):
        return "maybe"  # no sample since it was last off zero
    return True if t - first_zero > debounce else "maybe"


def _mission(flight: FlightData, t: int) -> tuple[Maybe, Maybe, Maybe]:
    """(landing, guided, needs a position) for Auto, from the navigation command running
    (CMD.CId, logged as each command starts)."""
    cmd = flight.table("CMD", ["TimeUS", "CId"])
    if cmd is None or cmd.num_rows == 0:
        return "maybe", "maybe", "maybe"
    times, ids = _ints(cmd, "TimeUS"), _ints(cmd, "CId")
    nav = (times <= t) & ((ids <= NAV_LAST) | np.isin(ids, (NAV_SCRIPT_TIME, NAV_ATTITUDE_TIME)))
    if not np.any(nav):
        return "maybe", "maybe", "maybe"
    running = int(ids[np.flatnonzero(nav)[np.argmax(times[nav])]])
    landing: Maybe = True if running in NAV_LAND else "maybe" if running == NAV_RTL else False
    return landing, running in NAV_GUIDED, running != NAV_ATTITUDE_TIME


def _ground(flight: FlightData, names: Mapping[str, Any], rules: Mapping[str, Any],
            mode: str, spec: Mapping[str, Any], t: int) -> Maybe:  # fmt: skip
    """should_disarm_on_failsafe: in the arming delay; landed; in Stabilize or Acro with
    the throttle at zero; in Auto only before the mission started, which is not logged."""
    if t - flight.arm_us < float(rules["arming_delay_s"]) * 1e6:
        return True
    landed = _landed(flight, names, t)
    if mode in ("AUTO", "AUTO_RTL"):
        return False if landed is False else "maybe"
    zero: Maybe = _throttle_zero(flight, t) if spec.get("manual_throttle") else False
    if landed is True or zero is True:
        return True
    return "maybe" if "maybe" in (landed, zero) else False


def _battery_unhealthy(flight: FlightData, t: int) -> Maybe:
    """Whether a monitor had no healthy reading (BAT.H) for the time it takes to raise a
    failsafe with no action."""
    bat = flight.table("BAT")
    if bat is None or bat.num_rows == 0 or "H" not in bat.column_names:
        return "maybe"
    times, inst, healthy = _ints(bat, "TimeUS"), _ints(bat, "Inst"), _ints(bat, "H")
    since = t - BATTERY_HEALTH_S * 1e6
    for i in np.unique(inst):
        rows = (inst == i) & (times >= since) & (times <= t)
        if not np.any(rows) or not np.any(healthy[rows]):
            return True
    return False


def _monitors(params: Mapping[str, float]) -> list[str]:
    return [p for p in MONITORS if int(params.get(f"{p}_MONITOR", 0)) != 0]


def battery_stages(flight: FlightData, earlier: Sequence[Sequence[str]], t: int) -> list[str]:
    """The stages a battery failsafe could have been raised at, most likely first: a
    first one low, a later one critical, and the no-action stage of a monitor gone quiet.
    Each monitor's stage only rises, so with one monitor a later failsafe is a higher
    stage than the one before could have been."""
    stages = ["critical", "low"] if earlier else ["low", "critical"]
    unhealthy = _battery_unhealthy(flight, t)
    if unhealthy is True:
        stages.insert(0, "unhealthy")
    elif unhealthy == "maybe":
        stages.append("unhealthy")
    if earlier and len(_monitors(flight.params)) == 1:
        floor = min(STAGE_RANK[x] for x in earlier[-1])
        stages = [x for x in stages if STAGE_RANK[x] > floor] or stages
    return stages


def _battery_actions(
    rule: Mapping[str, Any], params: Mapping[str, float], stages: Sequence[str]
) -> list[str]:
    """The actions those stages call for, on every monitor fitted."""
    actions: Mapping[str, str] = rule["actions"]
    out: list[str] = []
    for stage in stages:
        if stage == "unhealthy":
            out.append("NONE")
            continue
        for prefix in _monitors(params):
            value = params.get(f"{prefix}_{rule['battery'][stage]}")
            if value is not None:
                out.append(actions.get(str(int(value)), rule["otherwise"]))
    return list(dict.fromkeys(out)) or [str(rule["otherwise"])]


def _configured(
    o: Occurrence, rule: Mapping[str, Any], flight: FlightData, stages: Sequence[str]
) -> list[str]:
    """The action the failsafe's own setting asks for, before the firmware's deviations."""
    if o.kind == "FAILSAFE_BATT":
        return _battery_actions(rule, flight.params, stages)
    value = flight.params.get(str(rule["param"]))
    if value is None:
        return [str(rule["otherwise"])]
    return [str(rule["actions"].get(str(int(value)), rule["otherwise"]))]


def context(
    flight: FlightData,
    names: Mapping[str, Any],
    rules: Mapping[str, Any],
    changes: Sequence[Change],
    active: Mapping[Occurrence, Sequence[str]],
    kind: str,
    t: int,
) -> Context:
    """The state as a failsafe of `kind` fired at `t`. `active`: the failsafes still in
    force then, with the actions each could have been raised with."""
    mode = _mode_at(changes, t, str(rules["failsafes"][kind]["reason"]))
    spec: Mapping[str, Any] = rules["modes"].get(mode, {})
    landing: Maybe = spec.get("landing", False)
    guided: Maybe = spec.get("guided", False)
    position: Maybe = spec.get("position", "maybe")
    if mode == "AUTO":
        landing, guided, position = _mission(flight, t)
    lands: list[Maybe] = []
    for o, actions in active.items():
        if o.kind == "FAILSAFE_BATT":
            hits = [a in LANDING_PRIORITY for a in actions]
            lands.append(True if all(hits) else "maybe" if any(hits) else False)
    battery_lands: Maybe = True if True in lands else "maybe" if "maybe" in lands else False
    ctx = Context(
        mode=mode,
        ground=_ground(flight, names, rules, mode, spec, t),
        landing=landing,
        guided=guided,
        position=position,
        battery_lands=battery_lands,
        radio_active=any(o.kind == "FAILSAFE_RADIO" for o in active),
    )
    ctx.read += [("ERR", "Subsys"), ("ERR", "ECode"), ("MODE", "ModeNum"), ("MODE", "Rsn"),
                 ("ARM", "ArmState"), ("ARM", "Method"), ("EV", "Id")]  # fmt: skip
    if spec.get("manual_throttle"):
        ctx.read.append(("CTUN", "ThI"))
    if mode == "AUTO":
        ctx.read.append(("CMD", "CId"))  # the location policy withholds this evidence
    return ctx


def _option(params: Mapping[str, float], rules: Mapping[str, Any], name: str) -> bool:
    bits = int(params.get("FS_OPTIONS", 0))
    return bool(bits >> int(rules["options"][name]) & 1)


# where each FS_OPTIONS 'continue' bit applies, in the firmware's order of checks
CONTINUE_WHEN: Mapping[str, Callable[[Context, Mapping[str, Any]], Maybe]] = {
    "RC_CONTINUE_IF_AUTO": lambda ctx, spec: ctx.mode == "AUTO",
    "GCS_CONTINUE_IF_AUTO": lambda ctx, spec: ctx.mode == "AUTO",
    "RC_CONTINUE_IF_GUIDED": lambda ctx, spec: ctx.ask("guided"),
    "GCS_CONTINUE_IF_PILOT_CONTROL": lambda ctx, spec: not spec.get("autopilot", False),
}


def _landing(ctx: Context, also: Maybe, name: str) -> Maybe:
    """Landing, and a further condition (`name`) with it. What is open counts only when
    the two together stay open."""
    both = _both(ctx.landing, also)
    if both == "maybe":
        ctx.note(*(n for n, v in (("landing", ctx.landing), (name, also)) if v == "maybe"))
    return both


def _deviations(
    kind: str, rules: Mapping[str, Any], params: Mapping[str, float], ctx: Context,
    configured: Sequence[str],
) -> Iterator[tuple[Maybe, str]]:  # fmt: skip
    """The firmware's departures from the setting, in its order, each with whether it
    applied. Read lazily: the caller stops at one that surely applied, as the firmware
    does, so what the log leaves open counts only where the firmware would have looked."""
    rule: Mapping[str, Any] = rules["failsafes"][kind]
    if rule["landing_with_battery"]:
        yield _landing(ctx, ctx.battery_lands, "battery_lands"), "LAND"
    if _option(params, rules, "CONTINUE_IF_LANDING"):
        acts: Maybe = True
        if kind == "FAILSAFE_BATT":  # only when its own action is not None
            acts = (
                False if set(configured) == {"NONE"} else "maybe" if "NONE" in configured else True
            )
        if acts is not False:
            yield _landing(ctx, acts, "battery_stage"), "LAND"
    spec: Mapping[str, Any] = rules["modes"].get(ctx.mode, {})
    for name in rule["continue"]:
        if _option(params, rules, name):
            yield CONTINUE_WHEN[name](ctx, spec), "NONE"


def _ekf_actions(rule: Mapping[str, Any], params: Mapping[str, float], ctx: Context) -> list[str]:
    """failsafe_ekf_event: nothing when report-only, in Land, or in a mode that needs no
    position (unless FS_EKF_ACTION is 3); AltHold, or Land with the radio lost; else Land."""
    ekf: Mapping[str, int] = rule["ekf"]
    action = int(params.get(str(rule["param"]), 1))
    if action == ekf["report_only"] or ctx.mode == "LAND":
        return ["NONE"]
    everywhere = action == ekf["everywhere"]
    position = ctx.position if everywhere else ctx.ask("position")
    if position is False and not everywhere:
        return ["NONE"]
    quiet = ["NONE"] if position == "maybe" and not everywhere else []
    if action == ekf["althold"]:
        return [*quiet, "LAND" if ctx.radio_active else "ALTHOLD"]
    return [*quiet, "LAND"]


def _actions(
    kind: str, rules: Mapping[str, Any], params: Mapping[str, float], ctx: Context,
    configured: Sequence[str],
) -> list[str]:  # fmt: skip
    """Every action the firmware could have chosen, given what the log leaves open."""
    if kind == "FAILSAFE_EKFINAV":
        return _ekf_actions(rules["failsafes"][kind], params, ctx)
    out: list[str] = []
    ground = ctx.ask("ground")
    if ground is not False:
        out.append("DISARM")
        if ground is True:
            return out
    for applies, action in _deviations(kind, rules, params, ctx, configured):
        if applies is False:
            continue
        out.append(action)
        if applies is True:
            return out
    if len(configured) > 1:
        ctx.open.append("battery_stage")
    return out + list(configured)


def _chain(rules: Mapping[str, Any], action: str, ctx: Context) -> list[Outcome]:
    """The outcomes of one action: its modes in turn, each a fallback of the first; a
    mode the vehicle is already in logs no change, so reads as continuing."""
    chain = rules["chains"][action]
    if chain == "disarm":
        return [Outcome("disarm")]
    if not chain:
        return [Outcome("continue")]
    first = str(chain[0])
    return [
        Outcome("continue" if mode == ctx.mode else str(mode), None if i == 0 else first, mode)
        for i, mode in enumerate(chain)
    ]


def expected(
    kind: str, rules: Mapping[str, Any], params: Mapping[str, float], ctx: Context,
    configured: Sequence[str],
) -> list[Outcome]:  # fmt: skip
    out: list[Outcome] = []
    for action in _actions(kind, rules, params, ctx, configured):
        outcomes = [Outcome("disarm")] if action == "DISARM" else _chain(rules, action, ctx)
        out += [o for o in outcomes if o not in out]
    return out


def observed(
    rule: Mapping[str, Any],
    changes: Sequence[Change],
    disarms: Sequence[tuple[int, str]],
    t: int,
    window_us: float,
) -> tuple[str, int | None]:
    """The response the log shows: the first mode change with the failsafe's reason, or a
    disarm by it, within the window; else continuing."""
    seen = [
        (at, mode) for at, mode, reason in changes
        if t <= at <= t + window_us and reason == rule["reason"]
    ]  # fmt: skip
    methods = {rule.get("disarm"), "FAILSAFE_ACTION_TERMINATE", "AFS"} - {None}
    seen += [(at, "disarm") for at, how in disarms if t <= at <= t + window_us and how in methods]
    if not seen:
        return "continue", None
    at, what = min(seen)
    return what, at


def _disarms(flight: FlightData, names: Mapping[str, Any]) -> list[tuple[int, str]]:
    arm = flight.table("ARM")
    if arm is None or arm.num_rows == 0 or "Method" not in arm.column_names:
        return []
    methods: Mapping[str, str] = names.get("arming_methods", {})
    t, state, how = _ints(arm, "TimeUS"), _ints(arm, "ArmState"), _ints(arm, "Method")
    return [(int(t[k]), methods.get(str(int(how[k])), "")) for k in np.flatnonzero(state == 0)]


def _failsafes(flight: FlightData, reg: Registry) -> Result:
    if not flight.logged:
        return Result(skipped="a parameter file has no flight to judge")
    if flight.vehicle != "copter":
        return Result(skipped="the failsafe responses are modelled for Copter")
    rules: Mapping[str, Any] = reg.rules["failsafes"]
    names = reg.names(flight.firmware)
    if "mode_reasons" not in names:
        return Result(skipped="the knowledge pack has no mode-change reasons for this firmware")
    found = [
        o for o in occurrences(flight, names, tuple(rules["failsafes"]))
        if flight.arm_us <= o.start_us <= flight.disarm_us
    ]  # fmt: skip
    out = Result()
    if not found:
        return out  # nothing fired while armed: nothing to judge
    changes, disarms = _modes(flight, names), _disarms(flight, names)
    window = float(rules["window_s"]) * 1e6
    raised: dict[Occurrence, list[str]] = {}
    stages: list[list[str]] = []  # each earlier battery failsafe's
    for n, o in enumerate(found, 1):
        rule: Mapping[str, Any] = rules["failsafes"][o.kind]
        if o.kind == "FAILSAFE_BATT":
            stages.append(battery_stages(flight, stages, o.start_us))
        own = stages[-1] if o.kind == "FAILSAFE_BATT" else []
        configured = [] if "ekf" in rule else _configured(o, rule, flight, own)
        active = {
            a: actions for a, actions in raised.items()
            if a.start_us <= o.start_us and (a.end_us is None or a.end_us > o.start_us)
        }  # fmt: skip
        raised[o] = configured
        ctx = context(flight, names, rules, changes, active, o.kind, o.start_us)
        allowed = expected(o.kind, rules, flight.params, ctx, configured)
        what, at = observed(rule, changes, disarms, o.start_us, window)
        match = next((a for a in allowed if a.what == what), None)
        _report(flight, rule, Judged(o, n, ctx, what, at, match, allowed), out)
    return out


@dataclass(frozen=True)
class Judged:
    o: Occurrence
    n: int
    ctx: Context
    what: str  # the response the log shows
    at: int | None  # when it was logged
    match: Outcome | None  # the allowed outcome it was, if any
    allowed: Sequence[Outcome]

    @property
    def verdict(self) -> str:
        if self.match is None:
            return "unexpected"
        return "fell_back" if self.match.fallback_of else "as_configured"

    @property
    def wanted(self) -> str:
        """What the settings asked for first: the mode a fallback stood in for, or the
        first outcome allowed."""
        if self.match is not None and self.match.fallback_of:
            return self.match.fallback_of
        first = next((a for a in self.allowed if a.fallback_of is None), Outcome("continue"))
        return first.asked or first.what


def _setting(flight: FlightData, rule: Mapping[str, Any]) -> tuple[str, str]:
    """The setting that chose the action, and its value as the flight started."""
    names = (
        [str(rule["param"])] if "param" in rule else [f"BATT_{s}" for s in rule["battery"].values()]
    )
    values = [flight.params.get(n) for n in names]
    return ", ".join(names), ", ".join("" if v is None else f"{v:g}" for v in values)


def _report(flight: FlightData, rule: Mapping[str, Any], j: Judged, out: Result) -> None:
    setting, value = _setting(flight, rule)
    read = j.ctx.read + ([("BAT", "H")] if j.o.kind == "FAILSAFE_BATT" else [])
    at_s = round((j.o.start_us - flight.arm_us) / 1e6, 1)
    allowed = list(dict.fromkeys(a.what for a in j.allowed))
    ev = Evidence(
        id=f"failsafe:{j.n}",
        kind="fact",
        summary=(
            f"{j.o.kind} at {at_s} s in {j.ctx.mode}: {j.what} ({j.verdict.replace('_', ' ')}; "
            f"the settings allow {', '.join(allowed)})"
        ),
        messages=tuple(dict.fromkeys(m for m, _ in read)),
        fields=tuple(dict.fromkeys(f for _, f in read)),
        segment=Segment("failsafe", j.o.start_us, j.o.end_us or (j.at or j.o.start_us)),
        values={
            "failsafe": j.o.kind,
            "at_s": at_s,
            "lasted_s": None if j.o.end_us is None else round((j.o.end_us - j.o.start_us) / 1e6, 1),
            "mode_before": j.ctx.mode,
            "response": j.what,
            "wanted": j.wanted,
            "allowed": allowed,
            "setting": setting,
            "setting_value": value,
            "fs_options": int(flight.params.get("FS_OPTIONS", 0)),
            "left_open": j.ctx.open,
            "responded_after_s": None if j.at is None else round((j.at - j.o.start_us) / 1e6, 3),
        },
    )
    severity = {"as_configured": "info", "fell_back": "caution", "unexpected": "out"}[j.verdict]
    out.findings.append(
        Finding(
            code=f"failsafe.{j.verdict}",
            severity=severity,  # type: ignore[arg-type]
            confidence="medium" if j.ctx.open else "high",
            evidence=(ev,),
            params={
                "failsafe": rule["name"]["en"],
                "at_s": at_s,
                "mode": j.ctx.mode,
                "response": _did(j.what, j.ctx.mode),
                "wanted": _asked(j.wanted),
                "setting": setting,
                "value": value,
            },
        )
    )


def _did(what: str, mode: str) -> str:
    """What the aircraft did, as words for the finding's text."""
    if what == "continue":
        return f"stayed in {mode}"
    return "disarmed" if what == "disarm" else f"switched to {what}"


def _asked(what: str) -> str:
    """What the settings asked for, as words for the finding's text."""
    return {"continue": "carrying on", "disarm": "disarming"}.get(what, what)


FAILSAFES = Analyzer(
    name="failsafes",
    version=1,
    run=_failsafes,
    metrics=(),
    findings=("failsafe.as_configured", "failsafe.fell_back", "failsafe.unexpected"),
)
