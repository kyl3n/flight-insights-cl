"""Logging adequacy (spec: Analyzers common to all vehicles, "Logging adequacy").

Whether the log holds what the other analyzers need, and whether the logger kept up:

- pre-filter gyro data, without which the filters cannot be checked, and post-filter
  gyro data, which lets the filter prediction be checked against the aircraft;
- raw gyro samples lost (from the log checks made at import), which spoil spectra;
- messages the logger dropped (DSF.Dp) and stretches with no messages at all;
- messages other analyzers read that LOG_BITMASK leaves out.

Which bit logs which message, and the INS_RAW_LOG_OPT bits, come from the knowledge
pack's rules (knowledge/rules/logging.json); the bit names from the release's own
parameter metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    SEVERITY_OF_ZONE,
    Analyzer,
    CandidateChange,
    Evidence,
    Finding,
    MetricValue,
    Result,
)
from flight_insights.analyzers.registry import Registry


def _bit(value: int, bit: int) -> bool:
    return bool(value >> bit & 1)


def _armed(flight: FlightData, t: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    return (t >= flight.arm_us) & (t <= flight.disarm_us)


def _issues(flight: FlightData, kind: str, message: str | None = None) -> list[Mapping[str, Any]]:
    if flight.validation is None:
        return []
    return [
        i
        for i in flight.validation.get("issues", [])
        if i["kind"] == kind and (message is None or i["message"] == message)
    ]


def _gyro(flight: FlightData, reg: Registry, out: Result) -> None:
    raw: Mapping[str, Any] = reg.rules["logging"]["raw_gyro"]
    opt = int(flight.params.get("INS_RAW_LOG_OPT", 0))
    gyr = flight.table("GYR", ["TimeUS"])
    t = np.asarray(gyr.column("TimeUS").to_numpy(), np.int64) if gyr is not None else None
    logged = int(_armed(flight, t).sum()) if t is not None else 0
    post_only = _bit(opt, raw["post_filter"]) and not _bit(opt, raw["pre_and_post_filter"])
    bat_mask = int(flight.params.get("INS_LOG_BAT_MASK", 0))
    bat_opt = int(flight.params.get("INS_LOG_BAT_OPT", 0))
    # The batch sampler runs only while raw logging is off; its option bit 1 samples
    # after the filters, bit 2 before and after.
    batch_pre = bat_mask and flight.has("ISBH") and not (_bit(bat_opt, 1) and not _bit(bat_opt, 2))
    recommended = int(raw["recommended"])
    ev = Evidence(
        id="log:gyro",
        kind="parameter",
        summary=(
            f"{logged} raw gyro samples logged while armed; INS_RAW_LOG_OPT {opt}, "
            f"INS_LOG_BAT_MASK {bat_mask}"
        ),
        messages=("GYR", "ISBH"),
        fields=("INS_RAW_LOG_OPT", "INS_LOG_BAT_MASK", "INS_LOG_BAT_OPT"),
        segment=flight.armed,
        values={
            "gyr_samples": logged,
            "INS_RAW_LOG_OPT": opt,
            "INS_LOG_BAT_MASK": bat_mask,
            "INS_LOG_BAT_OPT": bat_opt,
            "batch_sampler": bool(flight.has("ISBH")),
        },
    )
    proposed = opt | recommended
    if not (logged and not post_only) and not batch_pre:
        out.findings.append(
            Finding(
                code="logging.no_prefilter_gyro",
                severity="caution",
                confidence="high",
                evidence=(ev,),
                params={"proposed": proposed},
                changes=(CandidateChange("INS_RAW_LOG_OPT", opt, proposed, "evidence"),),
            )
        )
    elif logged and not _bit(opt, raw["pre_and_post_filter"]):
        out.findings.append(
            Finding(
                code="logging.no_postfilter_gyro",
                severity="info",
                confidence="high",
                evidence=(ev,),
                params={"proposed": proposed},
                changes=(CandidateChange("INS_RAW_LOG_OPT", opt, proposed, "evidence"),),
            )
        )
    if not logged or flight.validation is None:
        return
    holes = _issues(flight, "imu sample loss", "GYR")
    lost = sum(int(i["count"]) for i in holes)
    percent = 100.0 * lost / (lost + logged)
    zone = reg.metrics["logging.imu_sample_loss"].zone(percent)
    loss = Evidence(
        id="log:gyr-loss",
        kind="metric",
        summary=f"about {lost} raw gyro samples missing against {logged} logged while armed",
        messages=("GYR",),
        fields=("SampleUS",),
        segment=flight.armed,
        values={
            "lost": lost,
            "logged": logged,
            "holes": [
                {k: i[k] for k in ("start_us", "end_us", "count", "instance")} for i in holes
            ],
        },
    )
    out.evidence.append(loss)
    out.metrics.append(MetricValue("logging.imu_sample_loss", "flight", percent, zone, loss.id))
    if zone in ("caution", "out"):
        out.findings.append(
            Finding(
                code="logging.samples_lost",
                severity=SEVERITY_OF_ZONE[zone],
                confidence="high",
                evidence=(loss,),
                params={"percent": float(f"{percent:.2g}"), "count": lost},
            )
        )


def _dropouts(flight: FlightData, reg: Registry, out: Result) -> None:
    dsf = flight.table("DSF", ["TimeUS", "Dp"])
    if dsf is None or dsf.num_rows == 0:
        return
    t = np.asarray(dsf.column("TimeUS").to_numpy(), np.int64)
    dp = np.maximum.accumulate(np.asarray(dsf.column("Dp").to_numpy(), np.int64))
    armed = _armed(flight, t)
    if not armed.any():
        return
    before = dp[t < flight.arm_us]
    # Dp counts from the start of the log file: what rose while armed is the flight's.
    count = int(dp[armed].max() - (before[-1] if len(before) else dp[armed][0]))
    zone = reg.metrics["logging.dropped_messages"].zone(count)
    ev = Evidence(
        id="log:dropouts",
        kind="metric",
        summary=f"the logger dropped {count} messages while armed",
        messages=("DSF",),
        fields=("Dp",),
        segment=flight.armed,
        values={"dropped": count},
    )
    out.evidence.append(ev)
    out.metrics.append(MetricValue("logging.dropped_messages", "flight", float(count), zone, ev.id))
    if zone in ("caution", "out"):
        out.findings.append(
            Finding(
                code="logging.dropouts",
                severity=SEVERITY_OF_ZONE[zone],
                confidence="high",
                evidence=(ev,),
                params={"count": count},
            )
        )


def _gaps(flight: FlightData, out: Result) -> None:
    gaps = _issues(flight, "timestamp gap")
    if not gaps:
        return
    longest = max((int(g["end_us"]) - int(g["start_us"])) / 1e6 for g in gaps)
    ev = Evidence(
        id="log:gaps",
        kind="fact",
        summary=(
            f"{len(gaps)} stretches with no log messages while armed, the longest {longest:.1f} s"
        ),
        messages=("all",),
        segment=flight.armed,
        values={"gaps": [{"start_us": g["start_us"], "end_us": g["end_us"]} for g in gaps]},
    )
    out.findings.append(
        Finding(
            code="logging.gap",
            severity="caution",
            confidence="high",
            evidence=(ev,),
            params={"seconds": round(longest, 1)},
        )
    )


def _missing(flight: FlightData, reg: Registry, out: Result) -> None:
    """Messages other analyzers need that LOG_BITMASK leaves out. A message missing
    although one of its bits is set has another cause, reported by its analyzer."""
    if "LOG_BITMASK" not in flight.params:
        return
    bitmask = int(flight.params["LOG_BITMASK"])
    vehicle = flight.vehicle
    missing: list[Mapping[str, Any]] = []
    for need in reg.rules["logging"]["needs"]:
        bits = need["bits"].get(vehicle)
        if bits is None or flight.has(need["message"]):
            continue
        if not any(_bit(bitmask, b) for b in bits):
            missing.append(need)
    if not missing:
        return
    adds = sorted({int(n["add"][vehicle]) for n in missing})
    proposed = bitmask
    for b in adds:
        proposed |= 1 << b
    meta = reg.pack.metadata(reg.pack.choose(flight.firmware or "0").version, vehicle)
    names: Mapping[str, str] = meta.get("LOG_BITMASK", {}).get("bitmask", {})
    bit_names = ", ".join(f"{names.get(str(b), 'bit')} (bit {b})" for b in adds)
    messages = ", ".join(n["message"] for n in missing)
    ev = Evidence(
        id="log:missing",
        kind="parameter",
        summary=f"no {messages} in the log, and LOG_BITMASK {bitmask} logs none of them",
        messages=tuple(n["message"] for n in missing),
        fields=("LOG_BITMASK",),
        values={"LOG_BITMASK": bitmask, "missing": [n["message"] for n in missing]},
    )
    out.findings.append(
        Finding(
            code="logging.missing_messages",
            severity="caution",
            confidence="high",
            evidence=(ev,),
            params={
                "messages": messages,
                "needed_for": "; ".join(n["for"] for n in missing),
                "proposed": proposed,
                "bits": bit_names,
            },
            changes=(CandidateChange("LOG_BITMASK", bitmask, proposed, "evidence"),),
        )
    )


def _log_adequacy(flight: FlightData, reg: Registry) -> Result:
    out = Result()
    _gyro(flight, reg, out)
    _dropouts(flight, reg, out)
    _gaps(flight, out)
    _missing(flight, reg, out)
    return out


LOG_ADEQUACY = Analyzer(
    name="logging",
    version=1,
    run=_log_adequacy,
    metrics=("logging.imu_sample_loss", "logging.dropped_messages"),
    findings=(
        "logging.no_prefilter_gyro",
        "logging.no_postfilter_gyro",
        "logging.samples_lost",
        "logging.dropouts",
        "logging.gap",
        "logging.missing_messages",
    ),
)
