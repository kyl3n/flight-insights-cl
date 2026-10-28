"""The charge the battery failsafe keeps back for the way home (spec: Power and efficiency
analysis, "Return-energy margin"): `BATT_LOW_MAH` from the measured return, in place of
ArduPilot's rule of thumb of 20% of capacity.

- **The way home** RTL flies: the climb to `RTL_ALT_M` at `WP_SPD_UP` from the ground (the
  most it can take), the flight back at `RTL_SPEED_MS` (`WP_SPD` where that is 0,
  mode_rtl.cpp), and the descent at `WP_SPD_DN` at hover power. It starts from the rules'
  percentile of the farthest each flight went from where it armed (the no-GNSS analyzer's
  `no_gnss:range`, kept on the laptop), into the same percentile of the airframe's
  measured winds as a headwind. A setting the flight does not hold is ArduCopter 4.7.0's
  default.
- **Its charge:** the power model's power on each part over the lowest voltage the pack
  reaches before its failsafe (so the current errs high), times each part's time.
- **The reserve** on top: the setup's reserve share of the pack's capacity (20%
  unanswered), left when it lands.
- **Each pack:** on parallel packs with a monitor each, each pack's failsafe keeps the
  return's share in proportion to its capacity, and its own reserve.
- **Proposed** when the failsafe keeps less, rounded up: a safety finding. It is only
  ever raised (the plan's never-lower rule). When the return and its reserve come to more
  than the rules' share of the packs, no failsafe setting covers it: a finding says the
  farthest trips are past what the packs bring home with their reserve.
- Low confidence until checked against the airframe's own flights to reserve.
- The distance itself stays out of the findings, which are kept with the airframe's
  recommendations and synced: only the charge it takes is shown.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.battery_power import PREFIXES, monitor_type
from flight_insights.analyzers.registry import Registry
from flight_insights.performance.advice import EVIDENCE, Context
from flight_insights.performance.top_speed import lowest_volts
from flight_insights.store.db import Store

LOW = "performance.low_mah"
SHORT = "performance.return_short"
RANGE = "no_gnss:range"


def farthest(store: Store, airframe_id: str) -> list[float]:
    """The farthest each flight of the airframe went from where it armed, in metres, from
    its latest analysis."""
    out: list[float] = []
    for (flight_id,) in store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ?", (airframe_id,)
    ).fetchall():
        run = store.conn.execute(
            "SELECT evidence_json FROM analysis_run WHERE flight_id = ? "
            "ORDER BY created_hlc DESC LIMIT 1",
            (flight_id,),
        ).fetchone()
        for e in json.loads(run[0]) if run else ():
            value = e.get("values", {}).get("farthest_m") if e["id"] == RANGE else None
            if isinstance(value, int | float):
                out.append(float(value))
    return out


def return_mah(ctx: Context, params: Mapping[str, float], distance_m: float,
               headwind_ms: float) -> tuple[float, float] | None:  # fmt: skip
    """The charge (mAh) RTL takes home from `distance_m` into `headwind_ms`, and the
    airspeed it flies back at; None when it has no speed to fly home at."""
    spec: Mapping[str, Any] = ctx.rules["return"]
    p: dict[str, float] = {**spec["defaults"], **params}
    home = float(p["RTL_SPEED_MS"]) or float(p["WP_SPD"])
    if home <= 0 or ctx.inputs.pack is None:
        return None
    climb, down, height = float(p["WP_SPD_UP"]), float(p["WP_SPD_DN"]), float(p["RTL_ALT_M"])
    air = home + headwind_ms

    def power(v: float, vz: float) -> float:
        return float(ctx.model.power(ctx.weight, v, vz, ctx.density, ctx.rules)[()])

    joules = (power(0.0, climb) * height / climb + power(air, 0.0) * distance_m / home
              + power(0.0, 0.0) * height / down)  # fmt: skip
    return joules / lowest_volts(params, ctx.inputs.pack) / 3.6, air


def _packs(reg: Registry, params: Mapping[str, float]) -> list[tuple[str, float]]:
    """Each monitored flight pack's parameter prefix and capacity (mAh)."""
    kinds = set(reg.rules["batteries"]["pack"])
    return [(prefix, float(params[f"{prefix}_CAPACITY"])) for i, prefix in enumerate(PREFIXES)
            if monitor_type(params, i) in kinds
            and float(params.get(f"{prefix}_CAPACITY", 0.0) or 0.0) > 0]  # fmt: skip


def charge_findings(ctx: Context, store: Store, reg: Registry, params: Mapping[str, float],
                    analysis: Mapping[str, Any]) -> list[dict[str, Any]]:  # fmt: skip
    """The battery failsafe's charge against the measured way home, for a flight's plan."""
    spec: Mapping[str, Any] = ctx.rules["return"]
    flight = store.get("flight", ctx.flight_id)
    packs = _packs(reg, params)
    if flight is None or not packs:
        return []
    far = farthest(store, str(flight["airframe_id"]))
    if not far:
        return []
    share = float(spec["percentile"])
    distance = float(np.percentile(far, share))
    wind = float(np.percentile(ctx.winds, share)) if ctx.winds else 0.0
    got = return_mah(ctx, params, distance, wind)
    if got is None:
        return []
    mah, air = got
    total = sum(c for _, c in packs)
    reserve = ctx.inputs.reserve_share
    present = {e["id"] for e in analysis.get("evidence", ())}
    evidence = [e for e in (EVIDENCE, RANGE) if e in present]
    common = {"return_mah": round(mah), "reserve_pct": round(100 * reserve),
              "mass_kg": round(ctx.mass_kg, 1), "wind_ms": round(wind, 1),
              "airspeed_ms": round(air, 1), "top_ms": round(ctx.top, 1),
              "flights": len(far)}  # fmt: skip

    def finding(code: str, key: str, params_: dict[str, Any],
                changes: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
        return {
            "id": f"performance:{key}:{ctx.flight_id}", "analyzer": "performance", "code": code,
            "severity": "safety", "confidence": "low", "params": common | params_,
            "evidence": evidence, "changes": changes, "stage": "operations",
            "origin": "measured",
        }  # fmt: skip

    need_share = mah / total + reserve
    if need_share > float(spec["max_share"]):
        return [finding(SHORT, "return_short", {
            "share_pct": round(100 * need_share), "max_pct": round(100 * float(spec["max_share"])),
            "capacity_mah": round(total)}, [])]  # fmt: skip
    step = float(spec["round_mah"])
    out: list[dict[str, Any]] = []
    for prefix, capacity in packs:
        param = f"{prefix}_LOW_MAH"
        current = float(params.get(param, 0.0) or 0.0)
        need = mah * capacity / total + reserve * capacity
        if need <= current:
            continue
        proposed = math.ceil(need / step) * step
        out.append(finding(LOW, f"low_mah:{prefix}", {
            "param": param, "current_mah": round(current), "proposed_mah": round(proposed),
            "capacity_mah": round(capacity), "pack_return_mah": round(mah * capacity / total),
        }, [{"param": param, "proposed": proposed, "stage": "operations"}]))  # fmt: skip
    return out
