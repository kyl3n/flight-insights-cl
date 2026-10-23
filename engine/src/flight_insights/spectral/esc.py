"""Motor speed from ESC telemetry, and how far it can be trusted (spec: The filter
analyzer in detail, "Reliable ESC RPM telemetry"; Motor and ESC health, "Telemetry
quality").

ESC.RPM is each motor's speed as the ESC reports it, already divided by the pole
pairs the autopilot was told; its fundamental vibration is RPM / 60 Hz. Telemetry
is unreliable when the ESC reports errors (ESC.Err, the share of bad frames), when
its updates stop for a while, or when a spinning motor reads zero. Some telemetry
carries no RPM at all (every reading zero): that ESC has no RPM to trust, which is
not a dropout.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from flight_insights.dataflash import si

DROPOUT = 5.0  # an update gap this many times the usual interval is a dropout


@dataclass(frozen=True)
class EscQuality:
    instance: int
    updates: int
    interval_ms: float  # median time between updates
    error_pct: float  # mean of ESC.Err; NaN when the ESC does not report it
    dropouts: int  # update gaps over DROPOUT x the usual interval
    longest_gap_ms: float
    zero_rpm: int  # updates reading 0 rpm in the spans, where every motor spins
    reports_rpm: bool  # False: this ESC's telemetry carries no RPM at all
    median_hz: float  # in the segments asked for


def quality(
    esc: pa.Table, start_us: int, end_us: int, hz_spans: list[tuple[int, int]]
) -> list[EscQuality]:
    """Per ESC, between two log times (the armed flight); motor Hz and zero readings
    over the given spans (steady hover)."""
    t = np.asarray(esc.column("TimeUS").to_numpy(), np.int64)
    inst = np.asarray(esc.column("Instance").to_numpy(), np.int64)
    rpm = si.values(esc, "RPM")
    err = si.values(esc, "Err") if "Err" in esc.column_names else np.full(len(t), np.nan)
    out: list[EscQuality] = []
    for i in np.unique(inst):
        sel = np.flatnonzero((inst == i) & (t >= start_us) & (t <= end_us))
        if len(sel) < 2:
            continue
        gaps = np.diff(t[sel]) / 1000.0
        usual = float(np.median(gaps))
        in_spans = np.zeros(len(sel), bool)
        for a, b in hz_spans:
            in_spans |= (t[sel] >= a) & (t[sel] <= b)
        held = rpm[sel][in_spans]
        spinning = held[held > 0]
        e = err[sel]
        out.append(
            EscQuality(
                instance=int(i),
                updates=len(sel),
                interval_ms=usual,
                error_pct=float(np.nanmean(e)) if np.isfinite(e).any() else float("nan"),
                dropouts=int((gaps > DROPOUT * usual).sum()),
                longest_gap_ms=float(gaps.max()),
                # An ESC that never reports RPM has none to lose.
                zero_rpm=int((held <= 0).sum()) if len(spinning) else 0,
                reports_rpm=bool((rpm[sel] > 0).any()),
                median_hz=float(np.median(spinning)) / 60.0 if len(spinning) else float("nan"),
            )
        )
    return out
