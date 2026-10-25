"""ArduPilot's harmonic notch: where its notches sit for given source frequencies
(libraries/Filter/HarmonicNotchFilter.cpp and AP_Vehicle::update_dynamic_notch, 4.7.0).

One harmonic notch set (INS_HNTCH_*, INS_HNTC2_*, INS_HNTC3_* or INS_HNTC4_*) places,
for each source frequency and each harmonic in its HMNCS bitmask, one notch, or two or
three for the double and triple options, spread around the centre. Notches above 48%
of the sample rate are switched off; below the minimum frequency (FREQ x FM_RAT) a notch
is held at the minimum, and faded out towards a quarter of it unless option bit 5 holds
it there.
The firmware caps the number of notches per gyro by board class.

The firmware also limits each update to a 5% move of a notch's centre; the
sample-by-sample simulation (simulate.py) models it, the spectrum-domain gain does
not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from flight_insights.filters.biquad import Biquad, notch, notch_a_q

NYQUIST_CUTOFF = 0.48  # HARMONIC_NYQUIST_CUTOFF
ATTENUATION_CUTOFF = 0.25  # NOTCHFILTER_ATTENUATION_CUTOFF
MAX_HARMONICS = 16  # HNF_MAX_HARMONICS
MAX_NOTCHES = 12  # INS_MAX_NOTCHES: sources per set
# HAL_HNF_MAX_FILTERS (AP_HAL_Boards.h) by board family
MAX_FILTERS = {"H7": 54, "SITL": 54, "Linux": 54, "F7": 27}
MAX_FILTERS_OTHER = 24

DOUBLE, MULTI_SOURCE, LOOP_RATE, ALL_IMUS, TRIPLE, LOW_AS_MIN, QUINTUPLE = (
    1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6,
)  # fmt: skip
FIXED, THROTTLE, RPM, ESC, FFT, RPM2 = 0, 1, 2, 3, 4, 5  # INS_HNTCH_MODE
# The sets, in the order the firmware applies them and indexes them in FTN, FTNS and
# FCN. 4.7.0 builds three on boards with more than 1 MB of flash and two on the rest
# (HAL_INS_NUM_HARMONIC_NOTCH_FILTERS); a set the board lacks has no parameters.
SETS = ("INS_HNTCH_", "INS_HNTC2_", "INS_HNTC3_", "INS_HNTC4_")


@dataclass(frozen=True)
class NotchParams:
    """One harmonic notch set's parameters (canonical names, as the flight started)."""

    prefix: str  # one of SETS
    enable: bool
    mode: int
    freq: float
    bw: float
    att: float
    harmonics: int
    ref: float
    opts: int
    fm_rat: float

    @staticmethod
    def read(params: Mapping[str, float], prefix: str) -> NotchParams:
        def get(name: str, default: float) -> float:
            return float(params.get(prefix + name, default))

        return NotchParams(
            prefix=prefix,
            enable=bool(get("ENABLE", 0)),
            mode=int(get("MODE", THROTTLE)),
            freq=get("FREQ", 80),
            bw=get("BW", 40),
            att=get("ATT", 40),
            harmonics=int(get("HMNCS", 3)),
            ref=get("REF", 0),
            opts=int(get("OPTS", 0)),
            fm_rat=get("FM_RAT", 1.0),
        )

    @property
    def composite(self) -> int:
        """HarmonicNotchFilterParams::num_composite_notches, then allocate_filters'
        cap of three (so quintuple runs as triple in 4.7.0)."""
        n = (
            2
            if self.opts & DOUBLE
            else 3
            if self.opts & TRIPLE
            else 5
            if self.opts & QUINTUPLE
            else 1
        )
        return min(n, 3)

    @property
    def num_harmonics(self) -> int:
        return bin(self.harmonics & 0xFFFF).count("1")

    def with_changes(self, **changes: float) -> NotchParams:
        values = {k: getattr(self, k) for k in self.__dataclass_fields__} | changes
        return NotchParams(**values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Placement:
    """Where the firmware asks one notch slot to sit, before its slew limit."""

    center_hz: float
    a: float


@dataclass(frozen=True)
class Notch:
    center_hz: float
    filter: Biquad


class HarmonicNotch:
    """One set's notches for one gyro at `sample_hz`."""

    def __init__(self, p: NotchParams, sample_hz: float, board_family: str | None):
        """HarmonicNotchFilter::init."""
        self.p = p
        self.sample_hz = sample_hz
        nyquist = sample_hz * NYQUIST_CUTOFF
        self.min_freq = p.freq * p.fm_rat
        center = min(max(p.freq, p.bw * 0.52), nyquist)
        self.spread = p.bw / (32 * center)
        self.a, self.q = notch_a_q(center, p.bw / p.composite, p.att)
        self.cap = MAX_FILTERS.get(board_family or "", MAX_FILTERS_OTHER)
        self.init_center = center  # where a fixed notch sits

    def placements(self, sources: Sequence[float]) -> list[Placement | None]:
        """HarmonicNotchFilter::update for these source frequencies: every notch slot
        in the firmware's order, with its centre and depth, or None where the slot is
        switched off. A slot keeps its place, and its state, as frequencies move."""
        nyquist = self.sample_hz * NYQUIST_CUTOFF
        n = len(sources)
        limit = min(n * self.p.num_harmonics * self.p.composite, self.cap)
        out: list[Placement | None] = []
        for i in range(n * MAX_HARMONICS):
            if len(out) >= limit:
                break
            harmonic, which = divmod(i, n)
            if not (1 << harmonic) & self.p.harmonics:
                continue
            centre = min(max(sources[which], 0.0), nyquist)
            spreads: list[float] = []
            if self.p.composite != 2:
                spreads.append(1.0)
            if self.p.composite > 1:
                spreads += [1.0 - self.spread, 1.0 + self.spread]
            out += [self._one(centre, s, harmonic + 1) for s in spreads]
        return out

    def notches(self, sources: Sequence[float]) -> list[Notch]:
        """The notches in force for these source frequencies."""
        out: list[Notch] = []
        for placed in self.placements(sources):
            if placed is None:
                continue
            f = notch(self.sample_hz, placed.center_hz, placed.a, self.q)
            if f is not None:
                out.append(Notch(placed.center_hz, f))
        return out

    def _one(self, centre: float, spread: float, harmonic: int) -> Placement | None:
        """HarmonicNotchFilter::set_center_frequency."""
        nyquist = self.sample_hz * NYQUIST_CUTOFF
        centre *= harmonic
        if centre >= nyquist:
            return None
        minimum = self.min_freq
        a = self.a
        if self.p.opts & LOW_AS_MIN:
            minimum *= harmonic
        else:
            disable = minimum * ATTENUATION_CUTOFF
            if centre < disable:
                return None
            if centre < minimum:  # fade towards no attenuation at the disable point
                a = a + (1.0 - a) * (centre - minimum) / (disable - minimum)
        return Placement(max(centre, minimum) * spread, a)
