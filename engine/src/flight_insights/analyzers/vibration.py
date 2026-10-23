"""Vibration and clipping (spec: Analyzers common to all vehicles; Reference range
library, "Vibration and estimator").

Reads VIBE: the accelerometers' vibration level per axis and the clipping count,
per IMU. Vibration is judged in steady hover, where it is comparable flight to
flight, and over the whole flight when there is no hover. Clipping counts over the
armed flight, since one hard manoeuvre can clip.
"""

from __future__ import annotations

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    SEVERITY_OF_ZONE,
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si

AXES = ("VibeX", "VibeY", "VibeZ")
STEADY_ENOUGH_S = 10.0  # this much hover makes a vibration figure high confidence
PERCENTILE = 95


def _vibration(flight: FlightData, reg: Registry) -> Result:
    vibe = flight.table("VIBE")
    if vibe is None or vibe.num_rows == 0:
        return Result(skipped="the log has no VIBE messages")
    t = np.asarray(vibe.column("TimeUS").to_numpy(), dtype=np.int64)  # log time, as segments
    imu = np.asarray(vibe.column("IMU").to_numpy(), dtype=np.int64)
    out = Result()

    hover = flight.hover()
    segments = hover or [flight.armed]
    kind = "hover" if hover else "flight"
    inside = np.zeros(len(t), dtype=bool)
    for s in segments:
        inside |= (t >= s.start_us) & (t <= s.end_us)
    span_s = sum(s.end_us - s.start_us for s in segments) / 1e6
    if inside.any():
        worst: tuple[float, str, int] | None = None
        per_axis: dict[str, float] = {}
        for axis in AXES:
            v = si.values(vibe, axis)
            for i in np.unique(imu[inside]):
                level = float(np.percentile(v[inside & (imu == i)], PERCENTILE))
                per_axis[f"{axis[-1]}{i}"] = round(level, 2)
                if worst is None or level > worst[0]:
                    worst = (level, axis[-1], int(i))
        assert worst is not None
        level, axis, which = worst
        metric = reg.metrics["vibration.p95"]
        zone = metric.zone(level)
        ev = Evidence(
            id=f"vibe:{kind}",
            kind="metric",
            summary=(
                f"95th-percentile vibration {level:.1f} m/s/s ({axis} axis, IMU {which}) "
                f"over {span_s:.1f} s of {kind}"
            ),
            messages=("VIBE",),
            fields=AXES,
            segment=Segment(kind, segments[0].start_us, segments[-1].end_us),
            values={"by_axis_and_imu": per_axis, "seconds": round(span_s, 1)},
        )
        out.evidence.append(ev)
        out.metrics.append(MetricValue("vibration.p95", kind, level, zone, ev.id))
        if zone in ("caution", "out"):
            confident = kind == "hover" and span_s >= STEADY_ENOUGH_S
            out.findings.append(
                Finding(
                    code="vibration.high",
                    severity=SEVERITY_OF_ZONE[zone],
                    confidence="high" if confident else "medium",
                    evidence=(ev,),
                    params={"value": round(level, 1), "axis": axis, "imu": which, "segment": kind},
                )
            )

    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    if armed.any() and "Clip" in vibe.column_names:
        clip = np.asarray(vibe.column("Clip").to_numpy(), dtype=np.int64)
        counts = {
            int(i): int(clip[armed & (imu == i)].max() - clip[armed & (imu == i)].min())
            for i in np.unique(imu[armed])
        }
        total = sum(counts.values())
        metric = reg.metrics["vibration.clipping"]
        zone = metric.zone(total)
        ev = Evidence(
            id="vibe:clip",
            kind="metric",
            summary=f"{total} accelerometer clipping events while armed",
            messages=("VIBE",),
            fields=("Clip",),
            segment=flight.armed,
            values={"by_imu": counts},
        )
        out.evidence.append(ev)
        out.metrics.append(MetricValue("vibration.clipping", "flight", float(total), zone, ev.id))
        if zone in ("caution", "out"):
            out.findings.append(
                Finding(
                    code="vibration.clipping",
                    severity=SEVERITY_OF_ZONE[zone],
                    confidence="high",
                    evidence=(ev,),
                    params={"count": total},
                )
            )
    return out


VIBRATION = Analyzer(
    name="vibration",
    version=1,
    run=_vibration,
    metrics=("vibration.p95", "vibration.clipping"),
    findings=("vibration.high", "vibration.clipping"),
)
