"""Hybrid logical clock timestamps for records (spec: Shared storage, "Two laptops
editing the same thing").

A stamp is wall-clock milliseconds, a counter and the laptop ID. Stamps from one
clock always increase, even when the wall clock steps back, and a stamp received
from another laptop moves this clock past it, so a change made after seeing
another always sorts after it. Stamps are fixed-width strings, so comparing the
strings compares the times; the laptop ID breaks ties between laptops.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from flight_insights.store.keys import check_laptop_id

MS_DIGITS = 13  # milliseconds since 1970, enough until the year 2286
COUNTER_DIGITS = 6  # hex
MAX_COUNTER = 16**COUNTER_DIGITS - 1
MAX_DRIFT_MS = 24 * 3600 * 1000


class ClockError(RuntimeError):
    """A stamp could not be made or accepted."""


@dataclass(frozen=True, order=True)
class Stamp:
    ms: int
    counter: int
    laptop_id: str

    def __str__(self) -> str:
        return f"{self.ms:0{MS_DIGITS}d}-{self.counter:0{COUNTER_DIGITS}x}-{self.laptop_id}"

    @classmethod
    def parse(cls, text: str) -> Stamp:
        try:
            ms, counter, laptop_id = text.split("-")
            if len(ms) != MS_DIGITS or len(counter) != COUNTER_DIGITS:
                raise ValueError(text)
            return cls(int(ms), int(counter, 16), check_laptop_id(laptop_id))
        except ValueError as e:
            raise ClockError(f"not a clock stamp: {text!r}") from e


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


class HybridClock:
    def __init__(self, laptop_id: str, wall: Callable[[], int] = wall_ms) -> None:
        self.laptop_id = check_laptop_id(laptop_id)
        self._wall = wall
        self._ms = 0
        self._counter = 0

    def now(self) -> str:
        """A stamp later than every stamp this clock has made or seen."""
        pt = self._wall()
        if pt > self._ms:
            self._ms, self._counter = pt, 0
        else:
            self._bump()
        return str(Stamp(self._ms, self._counter, self.laptop_id))

    def receive(self, remote: str) -> None:
        """Merge a stamp from another laptop, so the next local stamp sorts after it.

        A stamp too far ahead of this laptop's clock is refused rather than letting
        one laptop's wrong clock push every later stamp into the future.
        """
        r = Stamp.parse(remote)
        pt = self._wall()
        if r.ms > pt + MAX_DRIFT_MS:
            raise ClockError("a stamp from more than a day ahead of this laptop's clock")
        self._merge(r)

    def observe(self, stamp: str) -> None:
        """Start after a stamp already stored here, such as the newest in the database."""
        self._merge(Stamp.parse(stamp))

    def _merge(self, r: Stamp) -> None:
        if (r.ms, r.counter) > (self._ms, self._counter):
            self._ms, self._counter = r.ms, r.counter

    def _bump(self) -> None:
        if self._counter == MAX_COUNTER:
            # Over 16 million stamps in one millisecond: move to the next one.
            self._ms, self._counter = self._ms + 1, 0
        else:
            self._counter += 1
