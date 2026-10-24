"""The filter model on this flight (spec: The filter analyzer in detail, step 4).

Runs the flight's own filter settings over its logged pre-filter gyro data, sample by
sample, and compares the result with the post-filter data the firmware logged. A
close match says the app's predictions for other settings hold on this aircraft;
the filter analyzer builds its recommendations on the same model. The phase lag the
filters add at the control frequencies is reported with it.

Which metric the whole-spectrum error goes to depends on what the notches followed:
known exactly (no notch, a fixed one, or inputs logged at every update) or rebuilt
from rows the log holds less often than the firmware used them; each has its own
target. The control band's error is reported on every flight.
"""

from __future__ import annotations

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
    Segment,
)
from flight_insights.analyzers.registry import Registry
from flight_insights.filters import check

TABLES = ("GYR", "ESC", "MOTB", "FCN", "FCNS", "FTN", "FTNS", "RPM")


def _filter_model(flight: FlightData, reg: Registry) -> Result:
    if not flight.has("GYR"):
        return Result(skipped="the log has no raw gyro data")
    hover = flight.hover()
    spans = [(w.start_us, w.end_us) for w in hover] or [(flight.arm_us, flight.disarm_us)]
    kind = "hover" if hover else "flight"
    tables = {n: t for n in TABLES if (t := flight.table(n)) is not None}
    imu_count = flight.fingerprint.get("imu_count", {}).get("value")
    family = flight.fingerprint.get("board", {}).get("family")
    result = check(flight.params, tables, spans, imu_count, family)
    if result is None:
        return Result(skipped="no pre-filter gyro data in the flight")
    summary = result.summary()
    error = result.spectrum_error_db()
    ev = Evidence(
        id="filter:model",
        kind="series",
        summary=(
            f"gyro {result.instance} at {result.rate_hz:.0f} Hz over {summary['seconds']} s of "
            f"{kind} through the flight's filters"
            + (
                f": {error:.2f} dB from what the firmware logged"
                if error is not None
                else "; no post-filter data logged to compare with"
            )
        ),
        messages=("GYR", *(n for n in TABLES[1:] if n in tables)),
        fields=("GyrX", "GyrY", "GyrZ"),
        segment=Segment(kind, spans[0][0], spans[-1][1]),
        values=summary,
    )
    out = Result(evidence=[ev])
    if error is None:
        return out
    metric = (
        "filter.model_error_rebuilt" if result.notch_inputs == "rebuilt" else "filter.model_error"
    )
    zone = reg.metrics[metric].zone(error)
    out.metrics.append(MetricValue(metric, kind, error, zone, ev.id))
    control = result.control_band_error_db()
    control_zone = None
    if control is not None:
        control_zone = reg.metrics["filter.model_error_control"].zone(abs(control))
        out.metrics.append(
            MetricValue("filter.model_error_control", kind, abs(control), control_zone, ev.id)
        )
    off = [
        (value, which)
        for value, z, which in (
            (error, zone, "whole spectrum"),
            (abs(control) if control is not None else 0.0, control_zone, "control band"),
        )
        if z in ("caution", "out")
    ]
    if off:
        value, which = max(off)
        out.findings.append(
            Finding(
                code="filter.model_mismatch",
                severity="caution",  # the model, not the aircraft, is in question
                confidence="medium",
                evidence=(ev,),
                params={"error": round(value, 2), "which": which, "inputs": result.notch_inputs},
            )
        )
    return out


FILTER_MODEL = Analyzer(
    name="filter_model",
    version=2,
    run=_filter_model,
    metrics=("filter.model_error", "filter.model_error_rebuilt", "filter.model_error_control"),
    findings=("filter.model_mismatch",),
)
