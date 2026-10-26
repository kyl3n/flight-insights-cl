"""How a rate loop answers its target, from flight data (spec: Copter analyzers,
"Rate-loop tune").

The rate loop sits inside the attitude loop, so its target x is not set from outside:
the attitude controller adds its correction for the measured attitude, and through it
every gust that moves the measured rate y also moves x. Read directly, y against x
then mixes the rate loop with the attitude loop and the gusts: on a simulated loop in
5 deg/s of gusts the step's settling came out at 1 s instead of 110 ms. So each
response is read through an instrument i, a signal the pilot's stick sets and the
gusts do not: the rate of the attitude target, which the input shaping makes from the
stick alone. Whatever in x and y the instrument does not explain is left out.

Given stretches of i, x and y, sampled together:

- **Frequency response**, H = Giy / Gix (the joint input-output estimate of a closed
  loop's response; Welch cross-spectra, Hann windows, 50% overlap). Where the target
  is set from outside alone, it is the familiar Gxy / Gxx. Each bin's random error is
  estimated from the instrument's coherence with x and with y and the number of
  windows (Bendat and Piersol): the measured band runs up from the bottom to the first
  bin past the allowed error, and nothing above it is read. Coherence alone is not
  enough: a few windows of a weakly excited bin can show a high coherence by chance,
  and on a simulated quad, checked against a chirp, such bins were 20-70% off. From
  the measured band: the -3 dB bandwidth and the phase there, the peak gain, and the
  peak of |1 - H|, the error's response to the target, which is the sensitivity peak
  Ms of a loop whose controller acts on the error alone (no feed-forward). The peak
  counts only once |1 - H| has fallen back inside the band; one still rising at its
  top is above what the inputs measured.
- **Step response**: the impulse response, 1.5 s long at the full sample rate, from
  the instrumental-variable form of the Wiener-Hopf equations (the instrument's
  correlations with x and with y), with diagonal loading, summed into a step. With
  i = x it is the least-squares impulse response. It needs no window and loses nothing
  near zero frequency, where stick doublets carry little: on second-order loops with
  dead time inside an attitude loop it recovers rise time, overshoot and settling
  within a few percent. From the step: rise time (10-90%), overshoot, settling (within
  5%) and its final gain.

Intervals come from a seeded bootstrap over windows (frequency response) and blocks
(step response), so the same log gives the same interval on every laptop. Nothing is
decimated: every sample of every stretch is used.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np

Floats = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
Complex = np.ndarray[tuple[int, ...], np.dtype[np.complex128]]
Pick = np.ndarray[tuple[int], np.dtype[np.intp]]
HALF_POWER = 1.0 / np.sqrt(2.0)


@dataclass(frozen=True)
class Stretch:
    """Samples of the instrument, the target and the measured rate, taken together
    without a gap."""

    instrument: Floats
    target: Floats
    actual: Floats


@dataclass(frozen=True)
class CrossSpectra:
    """Each window's spectra, kept so the response can be resampled."""

    freqs: Floats
    gii: Floats  # (windows, bins)
    gxx: Floats
    gyy: Floats
    gix: Complex
    giy: Complex

    @property
    def windows(self) -> int:
        return len(self.gii)

    def response(self, pick: Pick | None = None) -> Response:
        sel = slice(None) if pick is None else pick
        gii, gxx, gyy = (g[sel].mean(axis=0) for g in (self.gii, self.gxx, self.gyy))
        gix, giy = self.gix[sel].mean(axis=0), self.giy[sel].mean(axis=0)
        windows = len(self.gii[sel])
        with np.errstate(divide="ignore", invalid="ignore"):
            h = np.where(np.abs(gix) > 0, giy / gix, 0)
            cx = np.where((gii > 0) & (gxx > 0), np.abs(gix) ** 2 / (gii * gxx), 0.0)
            cy = np.where((gii > 0) & (gyy > 0), np.abs(giy) ** 2 / (gii * gyy), 0.0)
            cx, cy = np.clip(cx, 0.0, 1.0), np.clip(cy, 0.0, 1.0)
            # the normalized random error of each transfer estimate, sqrt(1 - g) /
            # sqrt(2 n g) (Bendat and Piersol, Random Data), the two in quadrature
            error = np.sqrt((1 - cx) / (2 * windows * cx) + (1 - cy) / (2 * windows * cy))
        return Response(self.freqs, h, np.where(np.isfinite(error), error, np.inf))


@dataclass(frozen=True)
class Response:
    freqs: Floats
    h: Complex
    error: Floats  # each bin's estimated random error, as a share of |H|

    @property
    def gain(self) -> Floats:
        return np.abs(self.h)


def cross_spectra(stretches: Sequence[Stretch], rate_hz: float, n: int) -> CrossSpectra | None:
    """Welch cross-spectra over every whole window of n samples, or None if none fits."""
    window = np.hanning(n)
    spectra: list[tuple[Complex, Complex, Complex]] = []
    for s in stretches:
        for a in range(0, len(s.target) - n + 1, n // 2):
            i, x, y = (v[a : a + n] for v in (s.instrument, s.target, s.actual))
            spectra.append(
                (
                    np.fft.rfft((i - i.mean()) * window),
                    np.fft.rfft((x - x.mean()) * window),
                    np.fft.rfft((y - y.mean()) * window),
                )
            )
    if not spectra:
        return None
    i, x, y = (np.stack(v) for v in zip(*spectra, strict=True))
    return CrossSpectra(
        np.fft.rfftfreq(n, 1.0 / rate_hz),
        np.abs(i) ** 2,
        np.abs(x) ** 2,
        np.abs(y) ** 2,
        np.conj(i) * x,
        np.conj(i) * y,
    )


@dataclass(frozen=True)
class Margins:
    bandwidth_hz: float | None  # None: still above -3 dB at the top of the measured band
    phase_at_bandwidth_deg: float | None
    peak_db: float
    sensitivity_peak: float  # max |1 - H| over the measured band
    sensitivity_peak_hz: float  # where it is
    sensitivity_peak_inside: bool  # whether |1 - H| falls back before the band's top
    measured_to_hz: float  # the top of the measured band


def margins(
    r: Response,
    max_error: float,
    low_hz: float,
    high_hz: float,
    peak_drop: float = 0.9,
    peak_reach: float = 1.2,
) -> Margins | None:
    """What the measured band says: the bins from `low_hz` up to the first whose error
    is over `max_error`, or to `high_hz`. None when fewer than 3 are measured. The peak
    of |1 - H| is inside the band when the band reaches `peak_reach` times its frequency
    and |1 - H| falls to `peak_drop` of it on the way."""
    band = np.flatnonzero((r.freqs >= low_hz) & (r.freqs <= high_hz))
    bad = np.flatnonzero(r.error[band] > max_error)
    band = band[: bad[0]] if len(bad) else band
    if len(band) < 3:
        return None
    f, gain = r.freqs[band], r.gain[band]
    phase = np.degrees(np.unwrap(np.angle(r.h[band])))
    bandwidth = phase_there = None
    below = np.flatnonzero(gain < HALF_POWER)
    if len(below) and below[0] > 0:
        k = int(below[0])
        # between the last bin above and the first below, on a log-frequency scale
        g0, g1 = np.log(gain[k - 1]), np.log(gain[k])
        u = (np.log(HALF_POWER) - g0) / (g1 - g0)
        bandwidth = float(np.exp(np.log(f[k - 1]) + u * (np.log(f[k]) - np.log(f[k - 1]))))
        phase_there = float(phase[k - 1] + u * (phase[k] - phase[k - 1]))
    error = np.abs(1 - r.h[band])
    top = int(np.argmax(error))
    return Margins(
        bandwidth_hz=bandwidth,
        phase_at_bandwidth_deg=phase_there,
        peak_db=float(20 * np.log10(gain.max())),
        sensitivity_peak=float(error[top]),
        sensitivity_peak_hz=float(f[top]),
        sensitivity_peak_inside=bool(
            f[-1] >= peak_reach * f[top] and (error[top + 1 :] <= peak_drop * error[top]).any()
        ),
        measured_to_hz=float(f[-1]),
    )


@dataclass(frozen=True)
class StepFigures:
    rise_s: float | None
    overshoot_pct: float
    settling_s: float
    gain: float  # the step's final level, 1 when the rate settles where it was asked


@dataclass(frozen=True)
class Correlations:
    """Each block's correlations of the instrument with the target, lags -(taps - 1) to
    taps - 1, and with the measured rate, lags 0 to taps - 1, kept so the step can be
    resampled by block. A lag k pairs i(t) for t in the block with x(t + k) anywhere in
    the same stretch, so the blocks of a stretch add up to its whole correlation: cutting
    it into blocks shortens no lag."""

    rix: Floats  # (blocks, 2 taps - 1), lag -(taps - 1) first
    riy: Floats  # (blocks, taps)

    @property
    def blocks(self) -> int:
        return len(self.riy)


def _lagged(a: Floats, b: Floats, offset: int, lags: int, first: int) -> Floats:
    """sum_t a(t) b(t + offset + k) for k = first to first + lags - 1, zero past b's ends."""
    m = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.conj(np.fft.rfft(a, m)) * np.fft.rfft(b, m), m)  # c[k mod m]
    return c[(np.arange(first, first + lags) + offset) % m]


def correlations(stretches: Sequence[Stretch], block: int, taps: int) -> Correlations | None:
    """Correlations of every whole block of `block` samples (each stretch's remainder
    joins its last block), each stretch's means removed; None when no block fits."""
    rix: list[Floats] = []
    riy: list[Floats] = []
    for s in stretches:
        n = len(s.target)
        i, x, y = (v - v.mean() for v in (s.instrument, s.target, s.actual))
        cuts = list(range(0, n - block + 1, block))
        for a, e in zip(cuts, [*cuts[1:], n], strict=True):
            lo, hi = max(0, a - taps + 1), min(n, e + taps - 1)
            rix.append(_lagged(i[a:e], x[lo:hi], a - lo, 2 * taps - 1, -(taps - 1)))
            riy.append(_lagged(i[a:e], y[a:hi], 0, taps, 0))
    if not riy:
        return None
    return Correlations(np.stack(rix), np.stack(riy))


def step(c: Correlations, pick: Pick | None = None, loading: float = 1e-3) -> Floats | None:
    """The step response: the impulse response h that solves sum_j h_j Rix(k - j) =
    Riy(k) for k = 0 to taps - 1, with `loading` times Rix(0) added to the diagonal,
    summed. None when the instrument does not move with the target."""
    sel = slice(None) if pick is None else pick
    rix, riy = c.rix[sel].sum(axis=0), c.riy[sel].sum(axis=0)
    taps = len(riy)
    zero = float(rix[taps - 1])
    if not zero > 0:
        return None
    lags = np.arange(taps)[:, None] - np.arange(taps)[None, :] + taps - 1
    system = rix[lags] + loading * zero * np.eye(taps)
    impulse = cast(Floats, np.linalg.solve(system, riy))  # pyright: ignore[reportUnknownMemberType]
    return np.asarray(np.cumsum(impulse), np.float64)


def figures(step: Floats, rate_hz: float, settle_share: float = 0.3) -> StepFigures:
    """Rise time, overshoot and settling of a step that should end at its final level,
    taken as the mean of its last `settle_share` of the record."""
    tail = step[int(len(step) * (1 - settle_share)) :]
    final = float(tail.mean())
    dt = 1.0 / rate_hz
    if final <= 0:
        return StepFigures(None, 0.0, len(step) * dt, final)

    def crossing(level: float) -> float | None:
        above = np.flatnonzero(step >= level * final)
        if not len(above):
            return None
        k = int(above[0])
        if k == 0:
            return 0.0
        a, b = step[k - 1], step[k]
        return (k - 1 + (level * final - a) / (b - a)) * dt

    t10, t90 = crossing(0.1), crossing(0.9)
    outside = np.flatnonzero(np.abs(step - final) > 0.05 * final)
    return StepFigures(
        rise_s=None if t10 is None or t90 is None else t90 - t10,
        overshoot_pct=max(0.0, (float(step.max()) - final) / final * 100.0),
        settling_s=float((outside[-1] + 1) * dt) if len(outside) else 0.0,
        gain=final,
    )


def bootstrap(
    count: int,
    statistics: Callable[[Pick], Mapping[str, float | None]],
    names: Sequence[str],
    seed: int,
    resamples: int,
    level: float,
) -> dict[str, tuple[float, float] | None]:
    """The percentile interval of each named statistic over resamples of `count`
    windows or blocks, drawn with replacement; None for one that cannot be taken on
    most resamples."""
    rng = np.random.default_rng(seed)
    drawn = [statistics(rng.integers(0, count, size=count)) for _ in range(resamples)]
    tail = (1.0 - level) / 2.0
    out: dict[str, tuple[float, float] | None] = {}
    for name in names:
        values = [d.get(name) for d in drawn]
        kept = np.asarray([v for v in values if v is not None and np.isfinite(v)], np.float64)
        if len(kept) < resamples / 2:
            out[name] = None
            continue
        low, high = np.quantile(kept, [tail, 1.0 - tail])
        out[name] = (float(low), float(high))
    return out
