"""Fleet evidence (spec: Parameter history and change attribution, "Fleet evidence"):
when the same parameter change is made on several airframes of a platform, their shifts
are pooled. A change that moved a metric the same way on enough of them is ranked
higher wherever it appears, and the platform's map gains a confirmed link; one that
moved it the other way is ranked lower.

- **What counts as one airframe's evidence.** A shift (not inconclusive) whose change
  is the shift's top-ranked candidate, tied to the metric by the map, with medium or
  high confidence; or the only change at that boundary, linked or not, since nothing
  else changed with it. Each airframe counts once per change and metric.
- **The key.** The parameter, which way it was changed (up or down) and the metric;
  the vote is which way the metric moved.
- **Teams.** Each confirmed link names the teams its agreeing airframes fly for (an
  airframe with no team counts as unnamed), so a link seen by two teams reads as more
  than one team's habit.
- **Confirmed** when at least `min_airframes` other airframes agree and at most
  `max_against_share` of the votes disagree; **contradicted** the other way round. An
  airframe's own shifts never count towards its own ranking.

Numbers are in rules/attribution.json "fleet", uncalibrated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from flight_insights.analyzers.registry import Registry
from flight_insights.history.attribution import Candidate, Change, Shift, attribute
from flight_insights.store.db import Store

Key = tuple[str, int, str]  # parameter, which way it was changed (+1 up, -1 down), metric


@dataclass(frozen=True)
class Tally:
    up: frozenset[str]  # the airframes where the metric rose
    down: frozenset[str]  # and fell

    def votes(self, effect: int) -> tuple[int, int]:
        """(agreeing, disagreeing) for a move of this sign."""
        same, other = (self.up, self.down) if effect > 0 else (self.down, self.up)
        return len(same), len(other)


def _sign(x: float) -> int:
    return 1 if x > 0 else -1


def change_sign(c: Change) -> int | None:
    before, after = c.before, c.after
    if not isinstance(before, int | float) or not isinstance(after, int | float):
        return None
    return None if after == before else _sign(float(after) - float(before))


def evidence(s: Shift, classes: set[str]) -> list[Change]:
    """The changes a shift counts for: its top candidate when the map ties it and the
    shift is at least medium confidence, or its only change."""
    if s.verdict == "inconclusive" or not s.candidates:
        return []
    top = s.candidates[0]
    if top.change.kind != "parameter" or top.change.cls not in classes:
        return []
    alone = len(s.candidates) == 1
    linked = top.group is not None and s.confidence in ("high", "medium")
    return [top.change] if alone or linked else []


def tallies(store: Store, reg: Registry, platform_id: str, exclude: str | None = None
            ) -> dict[Key, Tally]:  # fmt: skip
    """Every key the platform's airframes (but `exclude`) give evidence for."""
    classes = set(reg.rules["attribution"]["ranking"]["candidate_classes"])
    up: dict[Key, set[str]] = {}
    down: dict[Key, set[str]] = {}
    for (af,) in store.conn.execute(
        "SELECT id FROM airframe WHERE platform_id = ? ORDER BY id", (platform_id,)
    ).fetchall():
        if af == exclude:
            continue
        for s in attribute(store, reg, af, fleet=False):
            for c in evidence(s, classes):
                way = change_sign(c)
                if way is None:
                    continue
                key = (c.name, way, s.metric)
                (up if s.effect > 0 else down).setdefault(key, set()).add(af)
    return {k: Tally(frozenset(up.get(k, ())), frozenset(down.get(k, ())))
            for k in up.keys() | down.keys()}  # fmt: skip


def verdict(agree: int, against: int, rules: Mapping[str, Any]) -> str | None:
    """ "confirmed", "contradicted" or None for this many votes each way."""
    least, share = int(rules["min_airframes"]), float(rules["max_against_share"])
    for a, b, name in ((agree, against, "confirmed"), (against, agree, "contradicted")):
        if a >= least and b <= share * (a + b):
            return name
    return None


def links(store: Store, reg: Registry, platform_id: str) -> list[dict[str, Any]]:
    """The platform's confirmed links: each parameter change that moved a metric the
    same way on enough airframes, and whether the map already holds it."""
    from flight_insights.history.attribution import links_to

    rules: Mapping[str, Any] = reg.rules["attribution"]["fleet"]
    team_of = dict(store.conn.execute(
        "SELECT id, team FROM airframe WHERE platform_id = ?", (platform_id,)
    ).fetchall())  # fmt: skip
    out: list[dict[str, Any]] = []
    for (param, way, metric), t in sorted(tallies(store, reg, platform_id).items()):
        for effect in (1, -1):
            agree, against = t.votes(effect)
            if verdict(agree, against, rules) != "confirmed":
                continue
            link = links_to(reg, param, metric)
            expected = None if link is None or link.sign == 0 else link.sign * way
            out.append({
                "param": param, "changed": "up" if way > 0 else "down", "metric": metric,
                "moved": "up" if effect > 0 else "down", "airframes": agree, "against": against,
                "teams": sorted({str(team_of[a]) for a in (t.up if effect > 0 else t.down)
                                 if team_of.get(a)}),
                "in_map": link is not None and expected in (None, effect),
                "link": None if link is None else link.group.group,
            })  # fmt: skip
    return out


def adjust(c: Candidate, metric: str, effect: float, found: Mapping[Key, Tally],
           rules: Mapping[str, Any]) -> Candidate:  # fmt: skip
    """A candidate weighed with the platform's other airframes: confirmed raises it,
    contradicted lowers it, and a change the map does not tie to the metric that the
    fleet confirmed counts as a learned link."""
    way = change_sign(c.change) if c.change.kind == "parameter" else None
    t = found.get((c.change.name, way, metric)) if way is not None else None
    if t is None:
        return c
    agree, against = t.votes(_sign(effect))
    fleet_rules: Mapping[str, Any] = rules["fleet"]
    said = verdict(agree, against, fleet_rules)
    if said is None:
        return replace(c, fleet=(agree, against))
    if said == "confirmed" and c.group is None:
        weight, timing = float(fleet_rules["learned"]), c.factors.get("timing", 1.0)
        return Candidate(c.change, weight * timing, "fleet",
                         {"strength": weight, "timing": timing, "fleet": 1.0},
                         (agree, against))  # fmt: skip
    factor = float(fleet_rules[said])
    return Candidate(c.change, c.score * factor, c.group, dict(c.factors) | {"fleet": factor},
                     (agree, against))  # fmt: skip
