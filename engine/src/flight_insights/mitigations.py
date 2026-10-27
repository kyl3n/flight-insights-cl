"""What would get the aircraft home without GNSS, ranked (spec: Flying without GNSS,
"Recommendations and recovery"; the catalogue in knowledge/mitigations/copter.json).

For one flight's setup, each mitigation in the catalogue that would change something:
what it closes here (the readiness areas with a gap and the flight's findings it
answers), whether it would make the fleet goal reachable, and its benefit, measured on
this fleet where the fleet has measured it, else as the catalogue expects it. A
mitigation already in place is left out.

**In place** (`check` in the catalogue):

- a position source (optical flow, external navigation): a usable source set uses it
  and that set is set 1. On another set with a switch it is listed only when, in set 1,
  it would make the goal reachable; with no switch, or named but not usable, it is
  listed with the work left;
- drag fusion: on, with the dead-reckoning failsafe able to act; not listed where the
  build or frame has no drag fusion;
- the pilot's fallback: the readiness review's area is ready;
- the compass: the heading area is ready and CompassMot was run, or the compass sits on
  a mast (platform setup);
- a parameter bit: set;
- a second receiver: GPS2_TYPE and GPS_AUTO_SWITCH both set.

**The goal** is the recovery card's (recovery_link.goal_met, the same check as
`airframe.recovery_card`'s `goal`), played on this flight's parameters. A mitigation
makes it reachable when the check fails now and passes with the mitigation's `effect`:
a first source set that keeps a position, dead reckoning on the wind, and the
parameters it needs where the flight has them off.

**Measured benefit** comes from the outages and drills the no-GNSS analyzer measured on
the latest analyses of this platform's flights, each classed by the horizontal sources
of the source set it was flown on and whether drag fusion was on (the readiness
review's evidence in the same analysis). For a position source, "with" is outages
flown on a set that uses it and "without" those flown on a set with no source but GNSS;
for drag fusion, both on GNSS alone, with drag fusion on and off. A side's hold is its
shortest (the time the estimator kept a position it could fly on), its drift the worst
per minute. The benefit is measured when both sides were flown on this airframe, else
on its platform; otherwise the catalogue's expected benefit is used, and any one side
that was measured is shown as it is.

**Ranking**, each rule breaking the ties of the one before:

1. what makes the goal reachable, before what does not;
2. a measured hold gain above zero (the hold with it less the hold without), larger
   first, equal gains by the lower worst drift per minute with it; then the expected
   benefit, by the catalogue's `levels` (keeping a position, buying time, keeping the
   heading, a pilot's control, fewer losses); then a measured gain of zero or less;
3. the cheapest work left, by the catalogue's `kinds` (settings, calibration, hardware,
   a companion computer, research);
4. the catalogue's order.

Every number in the result is one the analyses stored (holds, drift a minute) or a
count; the rest is the catalogue's wording and sources. No position is read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from flight_insights import recovery_link
from flight_insights.analyzers.no_gnss_readiness import HORIZONTAL, fitted
from flight_insights.analyzers.registry import LANGUAGES, Registry
from flight_insights.knowledge import KnowledgeError, Pack
from flight_insights.store.db import Store

FILE = "mitigations/copter.json"
AREA, OUTAGE = "no_gnss:readiness:", "no_gnss:outage:"
AREAS = ("position", "switching", "heading", "height", "wind", "failsafes", "pilot_fallback")
OUTAGE_FINDINGS = ("no_gnss.position_held", "no_gnss.position_lost")
CHECKS = ("source", "drag_fusion", "pilot_fallback", "compass", "bit", "second_receiver")
MEASURES = ("source", "drag_fusion")
EFFECTS = ("keeps_position", "dead_reckons", "params")
GRADES = ("documented", "research")
SENSORS = ("optical_flow", "external_nav", "beacon", "wheel_encoder")

Values = Mapping[str, Any]
Goal = Callable[[bool, bool, Mapping[str, float]], bool | None]


# --- the catalogue -----------------------------------------------------------------------


def load(pack: Pack, reg: Registry) -> dict[str, Any]:
    """The Copter catalogue, checked (`validate`)."""
    doc: dict[str, Any] = pack.json(FILE)
    validate(doc, reg)
    return doc


def _texts(where: str, text: Any) -> None:
    found: Mapping[str, Any] = cast(Mapping[str, Any], text) if isinstance(text, dict) else {}
    if not all(isinstance(found.get(lang), str) and str(found[lang]).strip() for lang in LANGUAGES):
        raise KnowledgeError(f"{where}: needs text in {', '.join(LANGUAGES)}")


def _cited(where: str, claim: Any, sources: Mapping[str, Any]) -> None:
    """A claim: its text in every language and at least one source the catalogue lists."""
    if not isinstance(claim, dict):
        raise KnowledgeError(f"{where}: a claim is text with its sources")
    found = cast(Mapping[str, Any], claim)
    _texts(where, found.get("text"))
    cited: Any = found.get("sources")
    if not isinstance(cited, list) or not cited:
        raise KnowledgeError(f"{where}: a claim with no source is left out")
    unknown = [str(s) for s in cast(list[Any], cited) if s not in sources]
    if unknown:
        raise KnowledgeError(f"{where}: no such source {', '.join(unknown)}")


def _list(value: Any) -> list[Any]:
    return list(cast(list[Any], value)) if isinstance(value, list) else []


def _map(value: Any) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}


def validate(doc: Mapping[str, Any], reg: Registry) -> None:
    """Refuses a catalogue with a claim that cites nothing, a source with no address,
    or a kind, level, check, area or finding the engine does not know."""
    kinds, levels, bounds = (
        _list(doc.get("kinds")),
        _list(doc.get("levels")),
        _list(doc.get("bounds")),
    )
    if not (kinds and levels and bounds):
        raise KnowledgeError(f"{FILE}: needs its kinds, levels and bounds")
    sources = _map(doc.get("sources"))
    for sid, raw in sources.items():
        s = _map(raw)
        if not (s.get("title") and str(s.get("url", "")).startswith("https://") and s.get("path")):
            raise KnowledgeError(f"{FILE}: source {sid} needs a title, its https URL and path")
    drill = _map(doc.get("drill"))
    if drill.get("procedure") not in reg.procedures:
        raise KnowledgeError(f"{FILE}: drill names no procedure the registry knows")
    _cited(f"{FILE}: drill", {"text": drill.get("about"), "sources": drill.get("sources")},
           sources)  # fmt: skip
    seen: set[str] = set()
    for raw in _list(doc.get("mitigations")):
        m = _map(raw)
        where = f"{FILE}: mitigation {m.get('id')}"
        if not m.get("id") or m["id"] in seen:
            raise KnowledgeError(f"{where}: needs an id of its own")
        seen.add(str(m["id"]))
        if m.get("kind") not in kinds or m.get("grade") not in GRADES:
            raise KnowledgeError(f"{where}: unknown kind or grade")
        _texts(f"{where}: title", m.get("title"))
        _cited(f"{where}: for_goal", m.get("for_goal"), sources)
        if not _list(m.get("needs")):
            raise KnowledgeError(f"{where}: needs what it needs")
        for i, part in enumerate(_list(m.get("needs"))):
            _cited(f"{where}: needs {i + 1}", part, sources)
        for i, part in enumerate(_list(m.get("limits"))):
            _cited(f"{where}: limit {i + 1}", part, sources)
        expected = _map(m.get("expected"))
        _cited(f"{where}: expected", expected, sources)
        if expected.get("level") not in levels:
            raise KnowledgeError(f"{where}: expected level must be one of {', '.join(levels)}")
        marks = _list(m.get("bounds"))
        if not marks or not set(marks) <= set(bounds):
            raise KnowledgeError(f"{where}: bounds must be some of {', '.join(bounds)}")
        check = _map(m.get("check"))
        if check.get("kind") not in CHECKS or (
            check.get("kind") == "source" and check.get("sensor") not in SENSORS
        ):
            raise KnowledgeError(f"{where}: unknown check {dict(check)}")
        measure = m.get("measure")
        if measure is not None and (
            _map(measure).get("kind") not in MEASURES
            or (
                _map(measure).get("kind") == "source" and _map(measure).get("sensor") not in SENSORS
            )
        ):
            raise KnowledgeError(f"{where}: unknown measure {measure}")
        effect = _map(m.get("effect"))
        if not set(effect) <= set(EFFECTS):
            raise KnowledgeError(f"{where}: an effect is one of {', '.join(EFFECTS)}")
        effect_sources = _list(m.get("effect_sources"))
        if effect and (not effect_sources or not set(effect_sources) <= set(sources)):
            raise KnowledgeError(f"{where}: an effect on the goal needs its sources")
        closes = _map(m.get("closes"))
        if not set(_list(closes.get("areas"))) <= set(AREAS):
            raise KnowledgeError(f"{where}: closes an unknown readiness area")
        for rule in _list(closes.get("findings")):
            if _map(rule).get("code") not in reg.findings:
                raise KnowledgeError(f"{where}: closes no finding the registry knows")


# --- the fleet's measured outages ------------------------------------------------------------


@dataclass(frozen=True)
class Outage:
    """One outage or drill as an analysis stored it, classed by what it was flown on."""

    airframe: str
    flight: str
    sensors: frozenset[str]  # the horizontal sources other than GNSS of the set in use
    drag: bool | None  # drag fusion on; None where the build or frame has none
    drill: bool
    kept_s: float  # how long it kept a position it could fly on
    throughout: bool  # it kept one all through the outage
    rate: float | None  # drift a minute, when GNSS came back trustworthy


def set_sensors(position: Values, rules: Mapping[str, Any]) -> dict[int, frozenset[str]]:
    """Each source set's horizontal sources other than GNSS, from the readiness review's
    position area (it holds every EK3_SRCn_* value)."""
    names: Mapping[str, Mapping[str, str]] = rules["sensors"]
    out: dict[int, frozenset[str]] = {}
    for n in rules["sets"]:
        found = {
            names[f][str(int(position[k]))]
            for f in HORIZONTAL
            if (k := f"EK3_SRC{n}_{f}") in position
            and position[k] is not None
            and str(int(position[k])) in names[f]
        }
        if any(f"EK3_SRC{n}_{f}" in position for f in HORIZONTAL):
            out[int(n)] = frozenset(found)
    return out


def outages_of(
    evidence: Mapping[str, Values], rules: Mapping[str, Any], airframe: str, flight: str
) -> list[Outage]:
    """The outages one analysis measured; none where its readiness review was skipped
    (what they were flown on is then unknown)."""
    position = evidence.get(f"{AREA}position")
    if position is None:
        return []
    sets = set_sensors(position, rules)
    drag = evidence.get(f"{AREA}wind", {}).get("drag_fusion")
    out: list[Outage] = []
    for eid, v in evidence.items():
        rest = eid[len(OUTAGE) :] if eid.startswith(OUTAGE) else ""
        if not rest.isdigit() or v.get("kept_s") is None:
            continue
        flown = int(v.get("source_set") or 1)
        out.append(Outage(
            airframe=airframe, flight=flight, sensors=sets.get(flown, frozenset()),
            drag=drag, drill=bool(v.get("drill")), kept_s=float(v["kept_s"]),
            throughout=bool(v.get("held") or v.get("kept_relative")),
            rate=evidence.get(f"{eid}:drift", {}).get("drift_m_per_min"),
        ))  # fmt: skip
    return out


def fleet_outages(store: Store, reg: Registry, airframe_id: str) -> list[Outage]:
    """Every outage and drill on the latest analyses of the flights of `airframe_id`'s
    platform (its own included)."""
    airframe = store.get("airframe", airframe_id)
    if airframe is None:
        return []
    marks = ", ".join("?" for _ in OUTAGE_FINDINGS)
    rows = store.conn.execute(
        "SELECT f.id, f.airframe_id, r.evidence_json FROM flight f "
        "JOIN airframe a ON a.id = f.airframe_id "
        "JOIN analysis_run r ON r.id = (SELECT id FROM analysis_run WHERE flight_id = f.id "
        "ORDER BY created_hlc DESC LIMIT 1) "
        f"WHERE a.platform_id = ? AND EXISTS (SELECT 1 FROM finding x WHERE x.run_id = r.id "
        f"AND x.code IN ({marks})) ORDER BY f.id",
        (airframe["platform_id"], *OUTAGE_FINDINGS),
    ).fetchall()
    rules: Mapping[str, Any] = reg.rules["no_gnss"]["readiness"]
    out: list[Outage] = []
    for flight, frame, raw in rows:
        evidence = {e["id"]: e.get("values", {}) for e in json.loads(raw)}
        out += outages_of(evidence, rules, str(frame), str(flight))
    return out


def drill_flights(store: Store, reg: Registry, airframe_id: str) -> list[str]:
    """The airframe's flights whose latest analysis measured a GNSS-off drill."""
    return sorted(
        {o.flight for o in fleet_outages(store, reg, airframe_id)
         if o.drill and o.airframe == airframe_id}
    )  # fmt: skip


@dataclass(frozen=True)
class Side:
    """Outages of one kind: how many, the shortest hold and the worst drift a minute."""

    outages: int
    hold_s: float
    throughout: bool  # every one kept a position all through: the hold is a lower bound
    drift_m_per_min: float | None

    def to_json(self) -> dict[str, Any]:
        return {"outages": self.outages, "hold_s": self.hold_s, "throughout": self.throughout,
                "drift_m_per_min": self.drift_m_per_min}  # fmt: skip


def side(outages: Sequence[Outage]) -> Side | None:
    if not outages:
        return None
    rates = [o.rate for o in outages if o.rate is not None]
    return Side(
        outages=len(outages), hold_s=min(o.kept_s for o in outages),
        throughout=all(o.throughout for o in outages),
        drift_m_per_min=max(rates) if rates else None,
    )  # fmt: skip


@dataclass(frozen=True)
class Benefit:
    """What the fleet measured with a mitigation and without it. `scope` is where both
    sides were flown (this airframe, else its platform); None when they were not both
    flown anywhere, and each side then says where it was flown, if at all."""

    scope: Literal["airframe", "platform"] | None
    with_: Side | None
    without: Side | None
    with_scope: Literal["airframe", "platform"] | None = None
    without_scope: Literal["airframe", "platform"] | None = None

    @property
    def gain_s(self) -> float | None:
        if self.scope is None or self.with_ is None or self.without is None:
            return None
        return self.with_.hold_s - self.without.hold_s

    def to_json(self) -> dict[str, Any]:
        def one(s: Side | None, scope: str | None) -> dict[str, Any] | None:
            return None if s is None else s.to_json() | {"scope": scope}

        return {
            "scope": self.scope,
            "with": one(self.with_, self.with_scope),
            "without": one(self.without, self.without_scope),
        }


def _split(measure: Mapping[str, Any], outages: Sequence[Outage]
           ) -> tuple[list[Outage], list[Outage]]:  # fmt: skip
    alone = [o for o in outages if not o.sensors]
    if measure["kind"] == "drag_fusion":
        return [o for o in alone if o.drag is True], [o for o in alone if o.drag is False]
    return [o for o in outages if measure["sensor"] in o.sensors], alone


def benefit(measure: Mapping[str, Any], outages: Sequence[Outage], airframe: str) -> Benefit:
    """Both sides on this airframe if it flew both, else on its platform if that did;
    otherwise each side where it was flown (this airframe first)."""
    mine = [o for o in outages if o.airframe == airframe]
    here_with, here_without = _split(measure, mine)
    if here_with and here_without:
        return Benefit("airframe", side(here_with), side(here_without), "airframe", "airframe")
    all_with, all_without = _split(measure, outages)
    if all_with and all_without:
        return Benefit("platform", side(all_with), side(all_without), "platform", "platform")

    def one(here: list[Outage], anywhere: list[Outage]
            ) -> tuple[Side | None, Literal["airframe", "platform"] | None]:  # fmt: skip
        if here:
            return side(here), "airframe"
        return (side(anywhere), "platform") if anywhere else (None, None)

    w, ws = one(here_with, all_with)
    wo, wos = one(here_without, all_without)
    return Benefit(None, w, wo, ws, wos)


# --- this flight's setup ------------------------------------------------------------------


@dataclass(frozen=True)
class Setup:
    """One flight's setup as its analysis judged it."""

    areas: Mapping[str, Values]  # the readiness review's areas: their evidence values
    findings: Sequence[Mapping[str, Any]]  # id, code, severity, confidence, params, evidence
    params: Mapping[str, float]
    answers: Mapping[str, Any]  # the platform setup's answers


@dataclass(frozen=True)
class State:
    listed: bool  # it would change something here
    left: str  # the kind of work left (the catalogue's kinds)
    status: str  # missing, partial, no_switch, on_switch
    on_sets: tuple[int, ...] = ()  # usable source sets that use it already


def _usable_with(setup: Setup, rules: Mapping[str, Any], sensor: str) -> list[int]:
    position = setup.areas.get("position", {})
    sets = set_sensors(position, rules)
    return [int(n) for n in position.get("usable_sets") or () if sensor in sets.get(int(n), ())]


def state(m: Mapping[str, Any], setup: Setup, rules: Mapping[str, Any]) -> State:
    """Whether the mitigation is in place, and the kind of work it leaves."""
    check: Mapping[str, Any] = m["check"]
    kind, a, p = check["kind"], setup.areas, setup.params
    full = str(m["kind"])
    if kind == "source":
        sensor = str(check["sensor"])
        using = _usable_with(setup, rules, sensor)
        if 1 in using:
            return State(False, "settings", "in_place", tuple(using))
        if using:  # usable on another set: settings only (a switch, or set 1)
            switched = bool(a.get("switching", {}).get("switch_channels"))
            return State(True, "settings", "on_switch" if switched else "no_switch",
                         tuple(using))  # fmt: skip
        ready = fitted(p, rules, sensor) and (sensor != "optical_flow"
                                               or fitted(p, rules, "rangefinder"))  # fmt: skip
        return State(True, "settings" if ready else full, "partial" if ready else "missing")
    if kind == "drag_fusion":
        drag = a.get("wind", {}).get("drag_fusion")
        if drag is None:  # no drag fusion in this build or frame
            return State(False, full, "unavailable")
        if drag:
            acts = bool(a.get("failsafes", {}).get("dead_reckoning_failsafe_acts"))
            return State(not acts, "settings", "in_place" if acts else "partial")
        return State(True, full, "missing")
    if kind == "pilot_fallback":
        ready = a.get("pilot_fallback", {}).get("status") == "ready"
        return State(not ready, "settings", "in_place" if ready else "missing")
    if kind == "compass":
        heading = a.get("heading", {})
        mount = setup.answers.get("compass_mounting")
        if heading.get("status") == "gap":
            return State(True, "settings", "partial")
        if heading.get("compassmot_done") or mount == "mast":
            return State(False, "settings", "in_place")
        if mount == "on the flight controller":
            return State(True, full, "missing")
        return State(True, "calibration", "partial")  # CompassMot not run
    if kind == "bit":
        on = int(p.get(str(check["param"]), 0)) >> int(check["bit"]) & 1
        return State(not on, "settings", "in_place" if on else "missing")
    # a second receiver: set up and switched to
    second = bool(p.get("GPS2_TYPE", 0))
    if second and p.get("GPS_AUTO_SWITCH", 1):
        return State(False, "settings", "in_place")
    fitted_two = second or int(setup.answers.get("gnss_count") or 0) >= 2
    return State(True, "settings" if fitted_two else full, "partial" if fitted_two else "missing")


def _sets_in(value: Any) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", str(value))]


def closes(m: Mapping[str, Any], setup: Setup, rules: Mapping[str, Any]
           ) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:  # fmt: skip
    """The readiness areas with a gap it addresses, and the flight's findings it answers."""
    areas = [
        {"area": name, "status": setup.areas[name].get("status")}
        for name in m["closes"].get("areas", ())
        if name in setup.areas and setup.areas[name].get("status") not in ("ready", "unavailable")
    ]
    sets = set_sensors(setup.areas.get("position", {}), rules)
    found: list[Mapping[str, Any]] = []
    for rule in m["closes"].get("findings", ()):
        for f in setup.findings:
            if f["code"] != rule["code"] or f in found:
                continue
            missing = str(f.get("params", {}).get("missing", ""))
            if "missing_any" in rule and not any(s in missing for s in rule["missing_any"]):
                continue
            uses = rule.get("set_uses")
            if uses and not any(uses in sets.get(n, ()) for n in _sets_in(f["params"].get("set"))):
                continue
            found.append(f)
    return areas, found


# --- the goal and the ranking ------------------------------------------------------------


def goal_check(failsafes: Mapping[str, Any], defaults: Mapping[str, float]) -> Goal:
    """The recovery card's goal check, for parameters and what keeps a position."""

    def met(keeps: bool, dead_reckons: bool, params: Mapping[str, float]) -> bool | None:
        return recovery_link.goal_met(failsafes, defaults, params, keeps, dead_reckons)

    return met


def goal_inputs(setup: Setup) -> tuple[bool, bool]:
    """What the recovery card plays: a first source set that keeps a position, and drag
    fusion for dead reckoning."""
    keeps = 1 in (setup.areas.get("position", {}).get("usable_sets") or ())
    return keeps, bool(setup.areas.get("wind", {}).get("drag_fusion"))


def makes_goal(m: Mapping[str, Any], setup: Setup, goal: Goal, now: bool | None) -> bool:
    """Whether the goal check, failing now, passes with the mitigation's effect: its
    source in set 1, dead reckoning on the wind, and the parameters it needs where the
    flight has them off."""
    effect: Mapping[str, Any] = m.get("effect") or {}
    if now is not False or not effect:
        return False
    keeps, dead = goal_inputs(setup)
    params = dict(setup.params)
    needed: Mapping[str, Any] = effect.get("params") or {}
    for name, value in needed.items():
        if not params.get(name):
            params[name] = float(value)
    return bool(goal(keeps or bool(effect.get("keeps_position")),
                     dead or bool(effect.get("dead_reckons")), params))  # fmt: skip


RULES = ("goal", "measured", "expected", "measured_no_gain")


def rank(
    doc: Mapping[str, Any], setup: Setup, rules: Mapping[str, Any], goal: Goal,
    outages: Sequence[Outage], airframe: str | None,
) -> dict[str, Any]:  # fmt: skip
    """The mitigations that would change something on this setup, ranked, each with the
    rule that placed it; and the goal as it stands."""
    keeps, dead = goal_inputs(setup)
    now = goal(keeps, dead, setup.params) if setup.areas else None
    kinds: list[str] = list(doc["kinds"])
    levels: list[str] = list(doc["levels"])
    sources: Mapping[str, Mapping[str, Any]] = doc["sources"]
    items: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    if not setup.areas:  # the readiness review was skipped: nothing to judge against
        return {"goal_now": None, "items": []}
    for order, m in enumerate(doc["mitigations"]):
        st = state(m, setup, rules)
        if not st.listed:
            continue
        reaches = makes_goal(m, setup, goal, now)
        if st.status == "on_switch" and not reaches:
            continue  # on a switch already: in set 1 it would change nothing more
        measured = (benefit(m["measure"], outages, airframe or "")
                    if m.get("measure") and airframe else None)  # fmt: skip
        gain = None if measured is None else measured.gain_s
        if gain is not None and gain > 0:
            drift = measured.with_.drift_m_per_min if measured and measured.with_ else None
            group, within = 0, (-gain, float("inf") if drift is None else drift)
        elif gain is not None:
            group, within = 2, (-gain, 0.0)
        else:
            group, within = 1, (float(levels.index(m["expected"]["level"])), 0.0)
        rule = "goal" if reaches else RULES[1 + group]
        area_gaps, answered = closes(m, setup, rules)
        cited = sorted({s for part in (m["for_goal"], *m["needs"], m["expected"], *m["limits"])
                        for s in part["sources"]} | set(m.get("effect_sources") or ()),
                       key=list(sources).index)  # fmt: skip
        entry = {
            "id": m["id"], "kind": m["kind"], "left": st.left, "status": st.status,
            "on_sets": list(st.on_sets), "grade": m["grade"], "title": m["title"],
            "for_goal": m["for_goal"]["text"], "makes_goal": reaches, "rule": rule,
            "basis": "measured" if gain is not None else "expected",
            "measured": None if measured is None else measured.to_json(),
            "expected": {"level": m["expected"]["level"], "text": m["expected"]["text"],
                         "sources": m["expected"]["sources"]},
            "bounds": list(m["bounds"]),
            "needs": [{"text": n["text"], "sources": n["sources"]} for n in m["needs"]],
            "limits": [{"text": n["text"], "sources": n["sources"]} for n in m["limits"]],
            "closes": {"areas": area_gaps, "findings": [f["id"] for f in answered]},
            "sources": [{"id": s, **sources[s]} for s in cited],
        }  # fmt: skip
        key = (0 if reaches else 1, group, *within, kinds.index(st.left), order)
        items.append((key, entry))
    return {"goal_now": now, "items": [e for _, e in sorted(items, key=lambda x: x[0])]}


__all__ = [
    "Benefit", "Outage", "Setup", "Side", "State", "benefit", "closes", "drill_flights",
    "fleet_outages", "goal_check", "load", "makes_goal", "outages_of", "rank", "side",
    "state", "validate",
]  # fmt: skip
