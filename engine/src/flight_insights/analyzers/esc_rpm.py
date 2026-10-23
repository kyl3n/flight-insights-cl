"""ESC RPM: can motor speed from ESC telemetry be trusted to steer the notch? (spec: The
filter analyzer in detail, "Reliable ESC RPM telemetry"; Motor and ESC health,
"Telemetry quality").

Three checks, which the filter analyzer's choice of tracking source rests on:

- the error rate each ESC reports (ESC.Err), against ESC Configurator's guidance;
- dropouts: telemetry updates that stop for a while, or a motor reading zero while
  every motor is spinning in steady hover;
- RPM against the gyro: the motor frequency ESC telemetry gives (RPM / 60) must be
  where the pre-filter gyro spectrum has its motor peak, or one of its harmonics.
  A peak at another ratio usually means the pole count the autopilot divides by is
  wrong, and a notch in ESC mode would sit at the wrong frequency.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.spectral import EscQuality, harmonic_of, layout, peaks, quality, runs, welch

LOW_HZ = 0.3  # motor peaks are searched from this share of the ESC motor frequency up
PEAKS = 5  # the strongest peaks looked at
CONFIDENT_DB = 10.0  # a peak this far above the floor makes the comparison high confidence
STEADY_ENOUGH_S = 10.0
# Where the autopilot is told the motors' pole count, and when that setting is in use.
POLE_PARAMS = (
    ("SERVO_BLH_POLES", ("SERVO_BLH_BDMASK", "SERVO_BLH_MASK", "SERVO_BLH_AUTO")),
    ("SERVO_FTW_POLES", ("SERVO_FTW_MASK",)),
    ("ESC_HW_POLES", ("ESC_HW_ENABLE",)),
)
DSHOT_TYPES = range(4, 8)  # MOT_PWM_TYPE DShot150 to DShot1200 use the BLHeli poles


def _poles(params: Mapping[str, float]) -> tuple[str, int] | None:
    """The pole-count parameter this aircraft's ESC RPM is scaled by, if one is clear."""
    in_use = [
        name
        for name, enables in POLE_PARAMS
        if name in params
        and (
            any(params.get(e, 0) for e in enables)
            or (name == "SERVO_BLH_POLES" and int(params.get("MOT_PWM_TYPE", 0)) in DSHOT_TYPES)
        )
    ]
    if len(in_use) != 1:
        return None
    return in_use[0], int(params[in_use[0]])


def _telemetry(
    flight: FlightData, reg: Registry, found: list[EscQuality], kind: str, out: Result
) -> float:
    errors = [q.error_pct for q in found if np.isfinite(q.error_pct)]
    spinning = [q.median_hz for q in found if np.isfinite(q.median_hz)]
    hz = float(np.median(spinning)) if spinning else float("nan")
    interval = float(np.median([q.interval_ms for q in found]))
    ev = Evidence(
        id="esc:telemetry",
        kind="metric",
        summary=(
            f"{len(found)} ESCs reporting every {interval:.0f} ms; "
            + (f"motors at {hz:.1f} Hz in {kind}" if np.isfinite(hz) else "no RPM reported")
        ),
        messages=("ESC",),
        fields=("RPM", "Err"),
        segment=flight.armed,
        values={
            "escs": [
                {
                    "instance": q.instance,
                    "updates": q.updates,
                    "interval_ms": round(q.interval_ms, 2),
                    "error_pct": None if np.isnan(q.error_pct) else round(q.error_pct, 3),
                    "dropouts": q.dropouts,
                    "longest_gap_ms": round(q.longest_gap_ms, 1),
                    "zero_rpm": q.zero_rpm,
                    "motor_hz": None if np.isnan(q.median_hz) else round(q.median_hz, 2),
                    "reports_rpm": q.reports_rpm,
                }
                for q in found
            ],
            "motor_hz": None if np.isnan(hz) else round(hz, 2),
        },
    )
    out.evidence.append(ev)
    if errors:
        worst = max(range(len(found)), key=lambda k: np.nan_to_num(found[k].error_pct, nan=-1))
        rate = found[worst].error_pct
        zone = reg.metrics["esc.telemetry_error_rate"].zone(rate)
        out.metrics.append(MetricValue("esc.telemetry_error_rate", "flight", rate, zone, ev.id))
        if zone in ("caution", "out"):
            out.findings.append(
                Finding(
                    code="esc.telemetry_errors",
                    severity=SEVERITY_OF_ZONE[zone],
                    confidence="high",
                    evidence=(ev,),
                    params={"percent": round(rate, 2), "esc": found[worst].instance + 1},
                )
            )
    lost = sum(q.dropouts + q.zero_rpm for q in found)
    zone = reg.metrics["esc.telemetry_dropouts"].zone(lost)
    out.metrics.append(MetricValue("esc.telemetry_dropouts", "flight", float(lost), zone, ev.id))
    if zone in ("caution", "out"):
        escs = [q.instance + 1 for q in found if q.dropouts + q.zero_rpm]
        out.findings.append(
            Finding(
                code="esc.telemetry_dropouts",
                severity=SEVERITY_OF_ZONE[zone],
                confidence="high",
                evidence=(ev,),
                params={"count": lost, "escs": ", ".join(map(str, escs))},
            )
        )
    return hz


def _against_gyro(flight: FlightData, motor_hz: float, out: Result) -> None:
    hover = flight.hover()
    gyr = flight.table("GYR")
    if gyr is None or not hover or not np.isfinite(motor_hz) or motor_hz <= 0:
        return
    imu_count = flight.fingerprint.get("imu_count", {}).get("value")
    instances = sorted({int(i) for i in np.unique(gyr.column("I").to_numpy())})
    gyros = layout(int(flight.params.get("INS_RAW_LOG_OPT", 0)), imu_count, instances)
    if not gyros.pre:
        return
    primary = gyros.pre[0]
    spectrum = welch([r for w in hover for r in runs(gyr, primary, w.start_us, w.end_us)])
    if spectrum is None:
        return
    found = peaks(spectrum, LOW_HZ * motor_hz, spectrum.rate_hz / 2)[:PEAKS]
    seconds = round(spectrum.seconds, 1)
    ev = Evidence(
        id="esc:gyro",
        kind="series",
        summary=(
            f"pre-filter gyro {primary} spectrum over {seconds} s of hover: "
            + (
                ", ".join(f"{p.freq_hz:.1f} Hz (+{p.prominence_db:.0f} dB)" for p in found)
                or "no peaks"
            )
            + f"; ESC RPM gives {motor_hz:.1f} Hz"
        ),
        messages=("GYR", "ESC"),
        fields=("GyrX", "GyrY", "GyrZ", "RPM"),
        segment=Segment("hover", hover[0].start_us, hover[-1].end_us),
        values={
            "instance": primary,
            "rate_hz": round(spectrum.rate_hz, 1),
            "resolution_hz": round(spectrum.resolution_hz, 3),
            "windows": spectrum.windows,
            "seconds": seconds,
            "motor_hz": round(motor_hz, 2),
            "peaks": [
                {
                    "hz": round(p.freq_hz, 2),
                    "level_db": round(p.level_db, 1),
                    "prominence_db": round(p.prominence_db, 1),
                    "harmonic": harmonic_of(p.freq_hz, motor_hz),
                }
                for p in found
            ],
        },
    )
    out.evidence.append(ev)
    if not found or any(harmonic_of(p.freq_hz, motor_hz) for p in found):
        return  # no motor peak to compare, or one sits where ESC RPM says
    top = found[0]
    ratio = top.freq_hz / motor_hz
    changes: tuple[CandidateChange, ...] = ()
    poles = _poles(flight.params)
    if poles is not None:
        name, current = poles
        proposed = current / ratio
        if abs(proposed - round(proposed)) < 0.1 and round(proposed) % 2 == 0 and proposed >= 2:
            changes = (CandidateChange(name, current, float(round(proposed)), "setup"),)
    confident = top.prominence_db >= CONFIDENT_DB and spectrum.seconds >= STEADY_ENOUGH_S
    out.findings.append(
        Finding(
            code="esc.rpm_mismatch",
            severity="out",
            confidence="high" if confident else "medium",
            evidence=(ev,),
            params={
                "peak_hz": round(top.freq_hz, 1),
                "motor_hz": round(motor_hz, 1),
                "ratio": round(ratio, 3),
            },
            changes=changes,
        )
    )


def _esc_rpm(flight: FlightData, reg: Registry) -> Result:
    esc = flight.table("ESC")
    if esc is None or esc.num_rows == 0 or "RPM" not in esc.column_names:
        return Result(skipped="the log has no ESC telemetry")
    out = Result()
    hover = flight.hover()
    spans = [(w.start_us, w.end_us) for w in hover] or [(flight.arm_us, flight.disarm_us)]
    found = quality(esc, flight.arm_us, flight.disarm_us, spans)
    if not found:
        return Result(skipped="no ESC telemetry while armed")
    motor_hz = _telemetry(flight, reg, found, "hover" if hover else "flight", out)
    _against_gyro(flight, motor_hz, out)
    return out


ESC_RPM = Analyzer(
    name="esc_rpm",
    version=1,
    run=_esc_rpm,
    metrics=("esc.telemetry_error_rate", "esc.telemetry_dropouts"),
    findings=("esc.telemetry_errors", "esc.telemetry_dropouts", "esc.rpm_mismatch"),
)
