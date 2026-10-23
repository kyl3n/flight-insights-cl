"""Run the firmware's gyro filters sample by sample on logged pre-filter gyro data.

ArduPilot's filters are biquads whose coefficients change only when the notch
frequencies are updated (every 5 ms by default). Between updates each biquad is a
fixed linear filter, so a run of samples is cut into blocks of constant
coefficients and filtered without a Python loop per sample:

- inside each block, the response to its own inputs is the block convolved with the
  biquad's impulse response (all blocks at once, by FFT);
- what carries from one block to the next is the last two inputs and outputs, an
  affine map of the previous block's, so the carries for all blocks come from one
  prefix scan of 2x2 affine maps;
- each block then adds the response to its carried state.

The result equals the biquad recursion to rounding. A notch's centre moves at most 5%
per update (NOTCH_MAX_SLEW in NotchFilter.cpp), and a filter starts from its first
input as the firmware's reset does. A slot switched off passes its input through.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from flight_insights.filters.biquad import Biquad, notch
from flight_insights.filters.harmonic import LOOP_RATE, Placement
from flight_insights.filters.predict import FilterChain
from flight_insights.spectral.gyro import Run

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
Ints = np.ndarray[tuple[int], np.dtype[np.int64]]
UPDATE_US = 5_000  # notch updates at 200 Hz (AP_Vehicle::update_dynamic_notch_at_specified_rate)
SLEW = 0.05
IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0)


def _impulse(c: Floats, length: int) -> tuple[Floats, Floats]:
    """Per block: g, the impulse response of 1 / A(z), and h, of B(z) / A(z)."""
    b0, b1, b2, a1, a2 = (c[:, k] for k in range(5))
    g = np.zeros((len(c), length + 2))
    g[:, 2] = 1.0  # g[n] lives at column n + 2; columns 0 and 1 are g[-2], g[-1] = 0
    for n in range(1, length):
        g[:, n + 2] = -a1 * g[:, n + 1] - a2 * g[:, n]
    h = b0[:, None] * g[:, 2:] + b1[:, None] * g[:, 1:-1] + b2[:, None] * g[:, :-2]
    return g, h


def biquads(x: Floats, starts: Ints, coeffs: Floats) -> Floats:
    """Filter `x` (n, axes) with block b running from starts[b] with coeffs[b]
    (b0, b1, b2, a1, a2); every block must hold at least two samples. The filter
    starts as if it had always seen x[0]."""
    n, axes = x.shape
    ends = np.append(starts[1:], n)
    lengths = ends - starts
    blocks, longest = len(starts), int(lengths.max())
    g, h = _impulse(coeffs, longest)
    # Each block's response to its own samples.
    pos = np.arange(longest)
    idx = np.minimum(starts[:, None] + pos[None, :], n - 1)
    inside = pos[None, :] < lengths[:, None]
    xb = np.where(inside[:, :, None], x[idx], 0.0)  # (B, L, axes)
    size = 1 << int(np.ceil(np.log2(2 * longest)))
    zs = np.fft.irfft(
        np.fft.rfft(xb, size, axis=1) * np.fft.rfft(h, size, axis=1)[:, :, None], size, axis=1
    )[:, :longest]
    # Carries: state entering block b is (y[-1], y[-2]) with known inputs x[-1], x[-2].
    b1, b2, a1, a2 = (coeffs[:, k] for k in (1, 2, 3, 4))
    first = starts
    x1 = x[np.maximum(first - 1, 0)]
    x2 = x[np.maximum(first - 2, 0)]
    c0 = b1[:, None] * x1 + b2[:, None] * x2  # f0 = c0 - a1 y1 - a2 y2
    c1 = b2[:, None] * x1  # f1 = c1 - a2 y1
    rows = np.arange(blocks)
    last, prev = lengths - 1, lengths - 2

    def gat(k: Ints) -> Floats:
        return g[rows, k + 2]

    # (y_last, y_prev) of block b = M_b @ (y1, y2) + v_b
    m = np.empty((blocks, 2, 2))
    m[:, 0, 0] = -(gat(last) * a1 + gat(last - 1) * a2)
    m[:, 0, 1] = -gat(last) * a2
    m[:, 1, 0] = -(gat(prev) * a1 + gat(prev - 1) * a2)
    m[:, 1, 1] = -gat(prev) * a2
    v = np.empty((blocks, 2, axes))
    v[:, 0] = zs[rows, last] + gat(last)[:, None] * c0 + gat(last - 1)[:, None] * c1
    v[:, 1] = zs[rows, prev] + gat(prev)[:, None] * c0 + gat(prev - 1)[:, None] * c1
    # Prefix scan of the affine maps: after it, (m[b], v[b]) maps the state entering
    # block 0 to the state leaving block b.
    step = 1
    while step < blocks:
        m_new, v_new = m.copy(), v.copy()
        m_new[step:] = m[step:] @ m[:-step]
        v_new[step:] = m[step:] @ v[:-step] + v[step:]
        m, v = m_new, v_new
        step *= 2
    entry = np.empty((blocks, 2, axes))
    entry[0] = np.stack([x[0], x[0]])
    entry[1:] = m[:-1] @ entry[0] + v[:-1]
    y1, y2 = entry[:, 0], entry[:, 1]
    x1 = np.where((first >= 1)[:, None], x1, x[0])
    x2 = np.where((first >= 2)[:, None], x2, x[0])
    f0 = b1[:, None] * x1 + b2[:, None] * x2 - a1[:, None] * y1 - a2[:, None] * y2
    f1 = b2[:, None] * x1 - a2[:, None] * y1
    g0 = g[:, 2 : 2 + longest]
    g1 = g[:, 1 : 1 + longest]
    y = zs + g0[:, :, None] * f0[:, None, :] + g1[:, :, None] * f1[:, None, :]
    out = np.empty_like(x)
    out[idx[inside]] = y[inside]
    return out


def _blocks(times_us: Ints, period_us: int) -> Ints:
    """Block starts where the update tick changes. A one-sample block joins the block
    before it (the first, the one after it): the firmware would update in between,
    one sample earlier or later."""
    tick = (times_us - times_us[0]) // period_us
    starts = np.flatnonzero(np.diff(tick, prepend=-1))
    lengths = np.diff(np.append(starts, len(times_us)))
    keep = lengths >= 2
    keep[0] = True
    if lengths[0] < 2 and len(starts) > 1:
        keep[1] = False
    return starts[keep].astype(np.int64)


def _slewed(centres: Floats) -> Floats:
    """Limit each move to 5% of the last centre, as NotchFilter::init_with_A_and_Q
    does; a switched-off slot (NaN) starts fresh when switched on again."""
    out = centres.copy()
    for k in range(1, len(out)):
        prev, now = out[k - 1], out[k]
        if np.isnan(prev) or np.isnan(now) or prev == 0:
            continue
        out[k] = min(max(now, prev * (1 - SLEW)), prev / (1 - SLEW))
    return out


def _coefficients(sample_hz: float, centres: Floats, depth: Floats, q: float) -> Floats:
    rows: list[tuple[float, float, float, float, float]] = []
    for c, a in zip(centres, depth, strict=True):
        f: Biquad | None = None if np.isnan(c) else notch(sample_hz, float(c), float(a), q)
        rows.append(IDENTITY if f is None else (f.b0, f.b1, f.b2, f.a1, f.a2))
    return np.asarray(rows, np.float64)


def _slot(placed: Sequence[Sequence[Placement | None]], slot: int) -> tuple[Floats, Floats]:
    """One notch slot's centre (NaN while switched off) and depth at each block."""
    centre = np.full(len(placed), np.nan)
    depth = np.ones(len(placed))
    for k, p in enumerate(placed):
        here = p[slot] if slot < len(p) else None
        if here is not None:
            centre[k], depth[k] = here.center_hz, here.a
    return centre, depth


def simulate(chain: FilterChain, run: Run, loop_rate_hz: float = 400.0) -> Run:
    """The run's samples through the chain: notch sets in order, then the low-pass."""
    x = run.samples.astype(np.float64)
    for s in chain.sets:
        period = int(1e6 / loop_rate_hz) if s.notch.p.opts & LOOP_RATE else UPDATE_US
        starts = _blocks(run.times_us, period)
        if len(starts) == 0 or len(x) < 2:
            continue
        sources: Sequence[Floats] = [
            s.sources.between(int(run.times_us[b]), int(run.times_us[b]))[-1] for b in starts
        ]
        placed = [s.notch.placements([float(v) for v in src]) for src in sources]
        slots = max((len(p) for p in placed), default=0)
        for slot in range(slots):
            centre, depth = _slot(placed, slot)
            coeffs = _coefficients(chain.sample_hz, _slewed(centre), depth, s.notch.q)
            if np.all(coeffs == np.asarray(IDENTITY)):
                continue
            x = biquads(x, starts, coeffs)
    if chain.lowpass is not None and len(x) >= 2:
        lp = chain.lowpass
        starts = np.array([0], np.int64)
        coeffs = np.array([[lp.b0, lp.b1, lp.b2, lp.a1, lp.a2]])
        # One block for the whole run would need an impulse response as long as the
        # run; constant coefficients split into 1024-sample blocks give the same.
        starts = np.arange(0, len(x) - 1, 1024, dtype=np.int64)
        if len(x) - starts[-1] < 2:
            starts = starts[:-1]
        x = biquads(x, starts, np.repeat(coeffs, len(starts), axis=0))
    return Run(run.start_us, run.end_us, run.rate_hz, x, run.times_us)
