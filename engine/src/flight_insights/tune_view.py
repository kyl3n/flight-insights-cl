"""The Tune view of one flight (spec: User interface, "Tune"; mock-up Tune): how closely
each axis follows the pilot's stick, against another tune-check flight of the same
airframe and payload group, and whether the plan improved the tune.

- **Which flights.** A tune-check flight is one the tune analyzer measured tracking
  error on. Flights are compared within a payload group (trends.py), never light against
  loaded. In another group than its own, "this flight" stands for that group's
  tune-check flight flown on the same tune: the same values of every parameter the
  attribution map ties to the metrics shown here (rules/attribution.json), the nearest
  in flying order. Learned values (the hover throttle) are not tied, so they split
  nothing. Unless a person picks another, the comparison is the latest tune-check flight
  before it on a different tune (before the last change that could move the tune), else
  the latest before it.
- **Tracking error:** attitude error on roll and pitch, the gate's measure, and rate
  tracking error on each axis, each with its 95% interval, compared as the AutoTune
  offer's checks compare them (recommend/measured.py), by the share its procedure asks.
- **Responses** of one axis: the step and frequency responses rebuilt from both logs as
  the rate-loop analyzer measured them (analyzers/rate_tune.py). The step comes with a
  per-sample interval over the same resampled blocks as its figures' intervals; the
  frequency response is given over its measured band only. The figures and their
  intervals are the flight's analysed metrics.
- **Stick inputs** of this flight, counted or left out with why, in seconds from arming.
- **The plan's verdict:** the airframe's AutoTune action, its checks as last graded (or
  still waiting), and the changes that prepared it.
- **The tune-check card:** how to fly the profile.

Nothing here leaves the laptop: the view is drawn by the app's own UI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData, FlightError
from flight_insights.analyzers.rate_tune import Responses, loop_hz, responses
from flight_insights.analyzers.registry import Registry
from flight_insights.analyzers.tune import found_inputs, rate_hz
from flight_insights.history.attribution import links_for
from flight_insights.history.persist import load_snapshot
from flight_insights.knowledge import Pack
from flight_insights.recommend import measured, records
from flight_insights.segment.excitation import AXES
from flight_insights.spectral.response import step
from flight_insights.store.db import Store
from flight_insights.trends import Flight, grouped_flights, metric_of, payloads

Array = np.ndarray[Any, Any]
FAMILIES = ("attitude_error", "tracking_error")  # the gate's measure first, then its guard
FIGURES = ("rise_time", "overshoot", "settling", "bandwidth", "sensitivity_peak")
OFFER = "tune.autotune_offer"
CARD = "logging.tune_evidence"  # its procedure flies the tune-check profile
CARD_KINDS = ("ground_check", "fly", "abort")


class TuneViewError(ValueError):
    """A flight, comparison or payload group the view cannot show."""


def shown(reg: Registry) -> set[str]:
    """The metrics the view shows, on every axis that has them."""
    names = (*FAMILIES, *FIGURES)
    return {f"tune.{n}_{a}" for n in names for a in AXES} & set(reg.metrics)


class _Tunes:
    """Each flight's values of the parameters that could move the metrics shown."""

    def __init__(self, store: Store, pack: Pack, reg: Registry) -> None:
        self.store, self.pack, self.reg = store, pack, reg
        self.metrics = shown(reg)
        self.moves: dict[str, bool] = {}
        self.seen: dict[str, dict[str, float]] = {}

    def _moves(self, name: str) -> bool:
        if name not in self.moves:
            self.moves[name] = any(k.metric in self.metrics for k in links_for(self.reg, name))
        return self.moves[name]

    def of(self, flight_id: str) -> dict[str, float]:
        if flight_id not in self.seen:
            row = self.store.get("flight", flight_id)
            values: dict[str, float] = {}
            if row is not None and row["param_snapshot_id"]:
                snapshot = load_snapshot(self.store, row["param_snapshot_id"])
                canonical = snapshot.dialect(self.pack).to_canonical(snapshot.values).values
                values = {n: float(v) for n, v in canonical.items() if self._moves(n)}
            self.seen[flight_id] = values
        return self.seen[flight_id]

    def same(self, a: str, b: str) -> bool:
        ta, tb = self.of(a), self.of(b)
        return ta.keys() == tb.keys() and all(
            abs(ta[n] - tb[n]) <= measured.TOLERANCE for n in ta
        )  # fmt: skip


def _measured(store: Store, run_id: str | None) -> bool:
    """Whether the flight's analysis measured tracking error: a tune-check flight."""
    if run_id is None:
        return False
    names = [f"tune.tracking_error_{a}" for a in AXES]
    marks = ", ".join("?" for _ in names)
    row = store.conn.execute(
        f"SELECT 1 FROM metric_value WHERE run_id = ? AND metric IN ({marks}) LIMIT 1",
        (run_id, *names),
    ).fetchone()
    return row is not None


def _value(store: Store, f: Flight | None, metric: str) -> dict[str, Any] | None:
    if f is None or f.run_id is None:
        return None
    found = metric_of(store, f.run_id, metric, None)
    if found is None:
        return None
    value, spread = found
    out: dict[str, Any] = {"value": round(value, 4)}
    for key in ("low", "high", "events"):
        if spread.get(key) is not None:
            out[key] = round(float(spread[key]), 4)
    return out


def _needs(reg: Registry) -> dict[str, dict[str, Any]]:
    """How the AutoTune offer's checks judge each family of metric: the check and the
    share it asks (attitude error at least 20% lower; rate error no worse by 20%)."""
    out: dict[str, dict[str, Any]] = {}
    for s in reg.procedures.get(OFFER, {}).get("steps", ()):
        for c in s.get("checks", ()):
            if c.get("check") in ("metric_improves", "metric_not_worse"):
                family = str(c["metric"]).removeprefix("tune.").rsplit("_", 1)[0]
                out.setdefault(family, {"check": c["check"], "by": float(c["by"])})
    return out


def _compare(
    direction: str, before: Mapping[str, Any], after: Mapping[str, Any], by: float
) -> dict[str, Any]:
    """The change from the comparison to this flight, judged as the gate's checks judge
    a band: better or worse by `by`, with the intervals apart."""

    def m(v: Mapping[str, Any], with_change: bool) -> measured.Measured:
        return measured.Measured(
            "", "", with_change, float(v["value"]), v.get("low"), v.get("high")
        )  # fmt: skip

    _, info = measured.improves({"by": by}, [m(before, False), m(after, True)], direction)
    band = info["bands"][0]
    lows = (before.get("low"), before.get("high"), after.get("low"), after.get("high"))
    apart = None
    if None not in lows:
        apart = float(after["high"]) < float(before["low"]) or float(after["low"]) > float(
            before["high"]
        )
    b = float(before["value"])
    change = (float(after["value"]) - b) / abs(b) if b else None
    return {
        "change": None if change is None else round(change, 4),
        "apart": apart,
        "verdict": band["verdict"],
    }


def _rows(
    store: Store, reg: Registry, this: Flight | None, compare: Flight | None
) -> list[dict[str, Any]]:
    needs = _needs(reg)
    out: list[dict[str, Any]] = []
    for family in FAMILIES:
        for axis in AXES:
            metric = f"tune.{family}_{axis}"
            if metric not in reg.metrics:
                continue
            a, b = _value(store, this, metric), _value(store, compare, metric)
            if a is None and b is None:
                continue
            row: dict[str, Any] = {"metric": metric, "family": family, "axis": axis,
                                   "this": a, "compare": b}  # fmt: skip
            if a is not None and b is not None and family in needs:
                row |= _compare(reg.metrics[metric].direction, b, a, needs[family]["by"])
            out.append(row)
    return out


def _step_band(r: Responses, rules: Mapping[str, Any], seed: int) -> tuple[Array, Array] | None:
    """Each sample's interval over resampled blocks: the draws the step figures'
    intervals are taken over (rate_tune._step, the same seed)."""
    rng = np.random.default_rng(seed)
    count, loading = r.corr.blocks, float(rules["loading"])
    drawn = [
        step(r.corr, rng.integers(0, count, size=count), loading)
        for _ in range(int(rules["resamples"]))
    ]
    kept = [d for d in drawn if d is not None and np.isfinite(d).all()]
    if len(kept) < len(drawn) / 2:
        return None
    tail = (1.0 - float(rules["level"])) / 2.0
    low, high = np.quantile(np.stack(kept), [tail, 1.0 - tail], axis=0)
    return low, high


def _rounded(values: Array, digits: int) -> list[float]:
    return [round(float(v), digits) for v in values]


def curves(flight: FlightData, reg: Registry, axis: str, band: bool) -> dict[str, Any]:
    """One axis's frequency and step responses for drawing, or why there are none."""
    hz = loop_hz(flight, reg)
    if isinstance(hz, str):
        return {"why": hz}
    r = responses(flight, reg, axis, hz)
    if r is None:
        return {"why": "too little stick flying on this axis to measure its responses"}
    rules: Mapping[str, Any] = reg.rules["tune"]["response"]
    resp, m = r.spectra.response(), r.margins
    gain = resp.gain
    keep = (resp.freqs >= float(rules["low_hz"])) & (resp.freqs <= m.measured_to_hz) & (gain > 0)
    out: dict[str, Any] = {
        "hz": round(hz, 1),
        "seconds": round(r.seconds, 1),
        "frequency": {
            "f": _rounded(resp.freqs[keep], 3),
            "db": _rounded(20.0 * np.log10(gain[keep]), 2),
            "measured_to_hz": round(m.measured_to_hz, 3),  # as the bins are given
            "bandwidth_hz": None if m.bandwidth_hz is None else round(m.bandwidth_hz, 2),
        },
    }
    s = r.step_response(rules)
    if s is None:
        out["step"] = None
        return out
    entry: dict[str, Any] = {"t": _rounded(np.arange(len(s)) / hz, 4), "y": _rounded(s, 4)}
    if band:
        found = _step_band(r, rules, int(rules["seed"][axis]) + 100)
        if found is not None:
            entry["low"], entry["high"] = _rounded(found[0], 4), _rounded(found[1], 4)
    out["step"] = entry
    return out


def inputs(flight: FlightData, reg: Registry) -> dict[str, Any]:
    """The flight's stick inputs on each axis, in seconds from arming, counted or left
    out with why; or why the log cannot show them."""
    found = found_inputs(flight, reg)
    if isinstance(found, str):
        return {"why": found}
    arm = flight.arm_us
    axes: dict[str, list[dict[str, Any]]] = {}
    sticks: list[float] = []
    for axis, i in found.items():
        axes[axis] = [
            {"start_s": round((e.start_us - arm) / 1e6, 2),
             "end_s": round((e.end_us - arm) / 1e6, 2),
             "counted": e.rejected is None, "reason": e.rejected,
             "stick": None if e.stick is None else round(e.stick, 2)}
            for e in i.events
        ]  # fmt: skip
        sticks += [e.stick for e in i.events if e.rejected is None and e.stick is not None]
    return {
        "armed_s": round((flight.disarm_us - arm) / 1e6, 2),
        "rate_hz": round(rate_hz(flight), 1),
        "axes": axes,
        "stick_median": round(float(np.median(sticks)), 2) if sticks else None,
        "saturation_checked": {a: i.saturation_checked for a, i in found.items()},
        "sticks_checked": {a: i.sticks_checked for a, i in found.items()},
    }


def _gate(store: Store, airframe_id: str) -> dict[str, Any] | None:
    """The airframe's AutoTune action: its checks as last graded (or still waiting for
    a flight), and the changes that prepared it."""
    recs = [r for r in records.history(store, airframe_id) if r["finding_code"] == OFFER]
    action = next((r for r in recs if r["param"] is None and r["status"] != "superseded"), None)
    if action is None:
        return None
    detail: Mapping[str, Any] = action["detail"]
    graded = detail.get("grade") or detail.get("grade_pending")
    checks: list[dict[str, Any]] = (
        list(graded["checks"]) if graded
        else [dict(c) | {"passed": None}
              for s in detail.get("procedure", ()) if s.get("kind") == "verify"
              for c in s.get("checks", ())]
    )  # fmt: skip
    return {
        "id": action["id"],
        "status": action["status"],
        "applied_flight": detail.get("applied_flight"),
        "graded_flight": graded["flight"] if graded else None,
        "pending": "grade_pending" in detail,
        "checks": checks,
        "changes": [
            {"param": r["param"], "value": r["proposed_value"], "status": r["status"]}
            for r in recs
            if r["param"] is not None and r["status"] not in ("superseded", "rejected")
        ],
    }


def _summary(f: Flight, group: str, which: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": f.id, "utc_us": f.utc_us, "group": group, "payload": f.payload}
    if which is not None:
        out["why"] = which
    return out


def tune(
    store: Store,
    pack: Pack,
    reg: Registry,
    flight_id: str,
    load: Callable[[str], FlightData],
    axis: str = "roll",
    compare_id: str | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """The Tune view of a flight; `load` opens a flight's log (the caller may keep a
    few open), `group` picks the payload group, `compare_id` the flight to compare."""
    if axis not in AXES:
        raise TuneViewError(f"axis must be one of {', '.join(AXES)}")
    row = store.get("flight", flight_id)
    if row is None:
        raise TuneViewError("no such flight")
    if row["airframe_id"] is None:
        raise TuneViewError("the flight is waiting for its airframe at import")
    airframe = str(row["airframe_id"])
    flights = grouped_flights(store, reg, airframe)
    order = {f.id: i for i, (f, _) in enumerate(flights)}
    if flight_id not in order:
        raise TuneViewError("analyse the flight first")
    flight, own = flights[order[flight_id]]
    checks = [(f, g) for f, g in flights if _measured(store, f.run_id)]
    described = payloads(store, reg, airframe)["groups"]
    with_checks = {g for _, g in checks}
    groups = [
        dict(d) | {"tune_checks": sum(g == d["group"] for _, g in checks)}
        for d in described
        if d["group"] in with_checks or d["group"] == own
    ]  # fmt: skip
    chosen = group or own
    if chosen not in {d["group"] for d in groups}:
        raise TuneViewError("pick a payload group with a tune-check flight")
    tunes = _Tunes(store, pack, reg)

    this: Flight | None = flight
    if chosen != own:
        same = [f for f, g in checks if g == chosen and tunes.same(f.id, flight_id)]
        here = order[flight_id]
        this = min(same, key=lambda f: (abs(order[f.id] - here), order[f.id] > here), default=None)
    candidates = [f for f, g in checks if g == chosen and (this is None or f.id != this.id)]
    compare: Flight | None = None
    why: str | None = None
    if compare_id is not None:
        compare = next((f for f in candidates if f.id == compare_id), None)
        if compare is None:
            raise TuneViewError("compare with a tune-check flight of the same payload group")
        why = "chosen"
    elif this is not None:
        before = [f for f in candidates if order[f.id] < order[this.id]]
        changed = [f for f in before if not tunes.same(f.id, this.id)]
        compare, why = (
            (changed[-1], "before_change") if changed
            else (before[-1], "same_tune") if before
            else (None, None)
        )  # fmt: skip

    figures = {
        name: {"this": _value(store, this, f"tune.{name}_{axis}"),
               "compare": _value(store, compare, f"tune.{name}_{axis}")}
        for name in FIGURES
        if f"tune.{name}_{axis}" in reg.metrics
    }  # fmt: skip
    out: dict[str, Any] = {
        "flight": flight_id,
        "group": chosen,
        "own_group": own,
        "groups": groups,
        "axis": axis,
        "this": None if this is None else _summary(this, chosen),
        "compare": None if compare is None else _summary(compare, chosen, why),
        "candidates": [
            _summary(f, chosen)
            | {
                "before": this is None or order[f.id] < order[this.id],
                "same_tune": this is not None and tunes.same(f.id, this.id),
            }
            for f in candidates
        ],
        "needs": _needs(reg),
        "rows": _rows(store, reg, this, compare),
        "figures": figures,
        "gate": _gate(store, airframe),
        "card": [
            {"kind": s["kind"], "text": dict(s["text"])}
            for s in reg.procedures.get(CARD, {}).get("steps", ())
            if s["kind"] in CARD_KINDS
        ],
    }
    for key, f in (("this", this), ("compare", compare)):
        if f is None:
            continue
        try:
            data = load(f.id)
        except FlightError as e:  # its log is not in the cache: the metrics still show
            out[key] |= {"inputs": {"why": str(e)}, "curves": {"why": str(e)}}
            continue
        out[key] |= {"inputs": inputs(data, reg), "curves": curves(data, reg, axis, key == "this")}
    out["labels"] = {
        fid: {"utc_us": f.utc_us, "group": g, "payload": f.payload}
        for fid in _named(out)
        if fid in order
        for f, g in (flights[order[fid]],)
    }
    return out


def _named(view: Mapping[str, Any]) -> set[str]:
    """Every flight the view names, for the UI to label."""
    ids = {str(view["flight"])}
    for key in ("this", "compare"):
        if view.get(key):
            ids.add(str(view[key]["id"]))
    ids |= {str(c["id"]) for c in view["candidates"]}
    gate: Mapping[str, Any] = view.get("gate") or {}
    checks: list[Mapping[str, Any]] = list(gate.get("checks", ()))
    for c in checks:
        # metric_vs_lightest lists only the groups' names while it waits for a second
        bands: list[Mapping[str, Any]] = [b for b in c.get("bands", ()) if isinstance(b, dict)]
        shifts: list[Mapping[str, Any]] = list(c.get("shifts", ()))
        named = [b.get(k) for b in bands for k in ("before_flight", "after_flight")]
        named += [c.get(k) for k in ("light_flight", "heavy_flight")]
        named += [x.get("flight") for x in shifts]
        ids |= {str(n) for n in named if n}
    ids |= {str(gate[k]) for k in ("applied_flight", "graded_flight") if gate.get(k)}
    return ids
