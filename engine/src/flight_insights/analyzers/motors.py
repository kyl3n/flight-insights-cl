"""What the motors are doing in steady hover, as the filter and ESC analyzers see it:
ESC telemetry and its quality, the pre-filter gyro spectrum and its motor peaks, the
motors' fundamental frequency and which harmonics carry energy, and whether ESC RPM
can be trusted to steer a notch (spec: The filter analyzer in detail, step 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from flight_insights.analyzers.flight import FlightData
from flight_insights.spectral import (
    EscQuality,
    Peak,
    Spectrum,
    harmonic_of,
    layout,
    peaks,
    quality,
    runs,
    welch,
)

LOW_HZ = 0.3  # motor peaks are searched from this share of the motor frequency up
PEAKS = 8  # the strongest peaks looked at
MAX_HARMONIC = 4
RELIABLE_ERROR_PCT = 1.0  # ESC Configurator: 1% or more is usually a hardware problem
MOTOR_PEAK_DB = 10.0  # a motor peak this far above the floor is clear noise to filter


@dataclass(frozen=True)
class MotorPicture:
    kind: str  # "hover" or "flight": where it was measured
    spans: list[tuple[int, int]]
    escs: list[EscQuality] = field(default_factory=list[EscQuality])
    esc_hz: float | None = None  # median motor frequency from ESC RPM
    instance: int | None = None  # the pre-filter gyro the spectrum is from
    spectrum: Spectrum | None = None
    peaks: list[Peak] = field(default_factory=list[Peak])
    fundamental_hz: float | None = None  # the motors' fundamental, however known
    fundamental_from: str | None = None  # "ESC RPM" or "gyro spectrum"
    harmonics: dict[int, Peak] = field(default_factory=dict[int, Peak])
    rpm_reliable: bool = False
    rpm_problems: list[str] = field(default_factory=list[str])

    @property
    def seconds(self) -> float:
        return self.spectrum.seconds if self.spectrum is not None else 0.0

    def motor_peak(self) -> Peak | None:
        """The fundamental's peak, if the spectrum shows it."""
        return self.harmonics.get(1)

    def clear_motor_noise(self) -> bool:
        return any(p.prominence_db >= MOTOR_PEAK_DB for p in self.harmonics.values())


def _spans(flight: FlightData) -> tuple[str, list[tuple[int, int]]]:
    hover = flight.hover()
    if hover:
        return "hover", [(w.start_us, w.end_us) for w in hover]
    return "flight", [(flight.arm_us, flight.disarm_us)]


def picture(flight: FlightData) -> MotorPicture:
    kind, spans = _spans(flight)
    escs: list[EscQuality] = []
    esc_hz: float | None = None
    esc = flight.table("ESC")
    if esc is not None and esc.num_rows and "RPM" in esc.column_names:
        escs = quality(esc, flight.arm_us, flight.disarm_us, spans)
        spinning = [q.median_hz for q in escs if np.isfinite(q.median_hz)]
        esc_hz = float(np.median(spinning)) if spinning else None
    instance: int | None = None
    spectrum: Spectrum | None = None
    found: list[Peak] = []
    gyr = flight.table("GYR")
    if gyr is not None and gyr.num_rows:
        imu_count = flight.fingerprint.get("imu_count", {}).get("value")
        instances = sorted({int(i) for i in np.unique(gyr.column("I").to_numpy())})
        gyros = layout(int(flight.params.get("INS_RAW_LOG_OPT", 0)), imu_count, instances)
        if gyros.pre:
            instance = gyros.pre[0]
            spectrum = welch([r for a, b in spans for r in runs(gyr, instance, a, b)])
    if spectrum is not None:
        low = LOW_HZ * esc_hz if esc_hz else 20.0
        found = peaks(spectrum, low, spectrum.rate_hz / 2)[:PEAKS]
    fundamental, source = _fundamental(esc_hz, found)
    harmonics: dict[int, Peak] = {}
    if fundamental:
        for p in found:
            k = harmonic_of(p.freq_hz, fundamental)
            if k is not None and k <= MAX_HARMONIC and k not in harmonics:
                harmonics[k] = p
    problems = _rpm_problems(escs, esc_hz, found)
    return MotorPicture(
        kind,
        spans,
        escs,
        esc_hz,
        instance,
        spectrum,
        found,
        fundamental,
        source,
        harmonics,
        bool(escs) and esc_hz is not None and not problems,
        problems,
    )


def _fundamental(esc_hz: float | None, found: list[Peak]) -> tuple[float | None, str | None]:
    if esc_hz:
        return esc_hz, "ESC RPM"
    if found:  # without RPM, the strongest peak stands for the motors
        return found[0].freq_hz, "gyro spectrum"
    return None, None


def _rpm_problems(escs: list[EscQuality], esc_hz: float | None, found: list[Peak]) -> list[str]:
    """Why ESC RPM cannot steer a notch; empty when it can."""
    if not escs:
        return ["no ESC telemetry"]
    out: list[str] = []
    if any(not q.reports_rpm for q in escs):
        out.append("some ESCs report no RPM")
    errors = [q.error_pct for q in escs if np.isfinite(q.error_pct)]
    if errors and max(errors) >= RELIABLE_ERROR_PCT:
        out.append(f"telemetry errors up to {max(errors):.1f}%")
    if any(q.dropouts or q.zero_rpm for q in escs):
        out.append("telemetry dropouts")
    if esc_hz and found and not any(harmonic_of(p.freq_hz, esc_hz) for p in found[:5]):
        out.append("RPM does not match the gyro's motor peak")
    return out
