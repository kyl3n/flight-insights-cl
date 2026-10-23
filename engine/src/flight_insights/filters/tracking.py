"""What drives a harmonic notch over the flight (AP_Vehicle::update_dynamic_notch and
update_throttle_notch, 4.7.0), rebuilt from the log.

Each mode has its own source of frequencies:

- fixed: FREQ, constrained as the firmware's init does;
- REF 0 in a tracking mode: FREQ itself;
- throttle: FREQ x sqrt(throttle / REF), the throttle being the motors' output
  (MOTB.ThrOut, get_throttle_out). With multi-source the firmware uses each motor's
  thrust, which the log does not hold; the average throttle stands in for it;
- ESC telemetry: the mean motor frequency (RPM / 60) x REF, or with multi-source each
  motor's own frequency, from ESC.RPM held between updates;
- RPM sensor: RPM x REF / 60 from the RPM message;
- in-flight FFT: the centres the firmware logged (FCN/FCNS), turned back into the
  frequencies they were placed for.

The firmware updates the notch at 200 Hz; the log holds these sources at their own
rates (MOTB at 10 Hz, ESC at its telemetry rate), so each value is held until the
next.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from flight_insights.dataflash import si
from flight_insights.filters.harmonic import (
    ESC,
    FFT,
    FIXED,
    MAX_NOTCHES,
    MULTI_SOURCE,
    RPM,
    RPM2,
    THROTTLE,
    HarmonicNotch,
)

Ints = np.ndarray[tuple[int], np.dtype[np.int64]]
Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
ESC_GRID_US = 10_000  # ESC frequencies are sampled on this grid
RPM_TIMEOUT_US = 1_000_000  # an ESC whose RPM is older than this gives no frequency


@dataclass(frozen=True)
class Sources:
    """Source frequencies at each update: `freqs` is (updates, sources)."""

    times_us: Ints
    freqs: Floats
    how: str

    def between(self, start_us: int, end_us: int) -> Floats:
        """The values in force between two times: those updated inside, and the one
        before the start."""
        if not len(self.times_us):
            return np.empty((0, self.freqs.shape[1]))
        first = max(int(np.searchsorted(self.times_us, start_us, side="right")) - 1, 0)
        last = int(np.searchsorted(self.times_us, end_us, side="right"))
        return self.freqs[first : max(last, first + 1)]


def _column(table: pa.Table, name: str) -> Floats:
    return np.asarray(si.values(table, name), np.float64)


def _times(table: pa.Table) -> Ints:
    return np.asarray(table.column("TimeUS").to_numpy(), np.int64)


def _constant(value: float, start_us: int, how: str) -> Sources:
    return Sources(np.array([start_us], np.int64), np.array([[value]]), how)


def _esc(esc: pa.Table, start_us: int, end_us: int, multi: bool, ref: float) -> Sources | None:
    t, inst = _times(esc), np.asarray(esc.column("Instance").to_numpy(), np.int64)
    rpm = _column(esc, "RPM")
    motors = [int(i) for i in np.unique(inst)][:MAX_NOTCHES]
    grid = np.arange(start_us, end_us + 1, ESC_GRID_US, dtype=np.int64)
    per_motor = np.zeros((len(grid), len(motors)))
    fresh = np.zeros((len(grid), len(motors)), bool)
    for k, m in enumerate(motors):
        sel = inst == m
        tm, rm = t[sel], rpm[sel]
        idx = np.searchsorted(tm, grid, side="right") - 1
        ok = idx >= 0
        held = np.where(ok, rm[np.maximum(idx, 0)], 0.0)
        age = np.where(ok, grid - tm[np.maximum(idx, 0)], np.iinfo(np.int64).max)
        fresh[:, k] = ok & (age <= RPM_TIMEOUT_US)
        per_motor[:, k] = np.where(fresh[:, k], held, 0.0) / 60.0
    if not motors:
        return None
    if multi:  # a stale ESC that once reported reads 0, which the notch then fades out
        return Sources(grid, per_motor, "ESC RPM, each motor")
    counts = fresh.sum(axis=1)
    mean = np.where(counts > 0, per_motor.sum(axis=1) / np.maximum(counts, 1), 0.0)
    return Sources(grid, (mean * ref)[:, None], "ESC RPM, mean of the motors x REF")


def _logged(tables: dict[str, pa.Table], set_index: int, notch: HarmonicNotch) -> Sources | None:
    """FFT mode: the centres logged for this set, turned back into source frequencies.
    The logged centre is the first notch of a source: its first harmonic, the lower
    one of a double notch."""
    p = notch.p
    first_harmonic = next((h + 1 for h in range(16) if p.harmonics >> h & 1), 1)
    spread = 1.0 - notch.spread if p.composite == 2 else 1.0
    for name, columns in (("FCN", [f"CF{k}" for k in range(1, 7)]), ("FCNS", ["CF"])):
        table = tables.get(name)
        if table is None or table.num_rows == 0:
            continue
        sel = np.asarray(table.column("I").to_numpy(), np.int64) == set_index
        present = [c for c in columns if c in table.column_names]
        if not sel.any() or not present:
            continue
        centres = np.column_stack([_column(table, c)[sel] for c in present])
        centres = np.nan_to_num(centres, nan=0.0)
        return Sources(_times(table)[sel], centres / spread / first_harmonic, f"logged {name}")
    return None


def sources(
    notch: HarmonicNotch,
    tables: dict[str, pa.Table],
    start_us: int,
    end_us: int,
    set_index: int = 0,
) -> Sources | None:
    """The source frequencies of one notch set over a stretch of the flight, or None
    when the log lacks what the mode follows."""
    p = notch.p
    if p.mode == FIXED:
        return _constant(notch.init_center, start_us, "fixed")
    if p.ref == 0:
        return _constant(p.freq, start_us, "REF 0: FREQ")
    if p.mode == THROTTLE:
        motb = tables.get("MOTB")
        if motb is None or "ThrOut" not in motb.column_names:
            return None
        thr = np.maximum(_column(motb, "ThrOut"), 0.0)
        how = "throttle (MOTB.ThrOut)" + (
            ", average for each motor" if p.opts & MULTI_SOURCE else ""
        )
        return Sources(_times(motb), (p.freq * np.sqrt(thr / p.ref))[:, None], how)
    if p.mode == ESC:
        esc = tables.get("ESC")
        if esc is None or "RPM" not in esc.column_names:
            return None
        return _esc(esc, start_us, end_us, bool(p.opts & MULTI_SOURCE), p.ref)
    if p.mode in (RPM, RPM2):
        rpm = tables.get("RPM")
        if rpm is None or "RPM" not in rpm.column_names or "I" not in rpm.column_names:
            return None
        sel = np.asarray(rpm.column("I").to_numpy(), np.int64) == (0 if p.mode == RPM else 1)
        freq = np.maximum(_column(rpm, "RPM")[sel], 0.0) * p.ref / 60.0
        return Sources(_times(rpm)[sel], freq[:, None], "RPM sensor x REF")
    if p.mode == FFT:
        return _logged(tables, set_index, notch)
    return None
