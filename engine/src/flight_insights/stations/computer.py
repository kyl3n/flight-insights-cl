"""This computer's facts, as the shell reads them, turned into profile answers.

The shell reads the facts (operating system, processor, memory, disk, power,
clock) because it runs natively on the computer; the engine checks them and
keeps the host name only as a keyed hash.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from typing import Any

HASH_CONTEXT = b"flight-insights host name v1\x00"
SOURCE = "detected: this computer"
GIB = 1024**3
TEXT_FACTS = ("os", "os_version", "cpu", "time_source")


class FactsError(ValueError):
    """The shell sent facts the engine cannot use."""


def hash_host(key: bytes, host: str) -> str | None:
    """A keyed hash of the host name, or None when there is none. Case and
    surrounding space are ignored, as Windows ignores them."""
    if len(key) < 32:
        raise ValueError("the hashing key must be at least 32 bytes")
    name = host.strip().casefold()
    if not name:
        return None
    digest = hmac.new(key, HASH_CONTEXT + name.encode(), hashlib.sha256).hexdigest()
    return f"host1-{digest}"


def _number(facts: dict[str, Any], name: str) -> float | None:
    v = facts.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise FactsError(f"{name} must be a non-negative number")
    return float(v)


def _gb(facts: dict[str, Any], name: str) -> float | None:
    v = _number(facts, name)
    return None if v is None else round(v / GIB, 1)


def computer_answers(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Answers for the computer and clock parts; a fact the shell could not read
    is left out, never guessed."""
    for name in TEXT_FACTS:
        if facts.get(name) is not None and not isinstance(facts[name], str):
            raise FactsError(f"{name} must be text")
    computer: dict[str, Any] = {
        "os": facts.get("os"),
        "os_version": facts.get("os_version"),
        "cpu": facts.get("cpu"),
        "memory_gb": _gb(facts, "memory_bytes"),
        "disk_gb": _gb(facts, "disk_bytes"),
        "disk_free_gb": _gb(facts, "disk_free_bytes"),
    }
    if (cores := _number(facts, "cpu_cores")) is not None:
        computer["cpu_cores"] = int(cores)
    if (mhz := _number(facts, "cpu_mhz")) is not None and mhz > 0:
        computer["cpu_mhz"] = round(mhz)
    if facts.get("power") is not None:
        if facts["power"] not in ("battery", "mains"):
            raise FactsError("power must be battery or mains")
        computer["power"] = facts["power"]
    clock: dict[str, Any] = {"time_source": facts.get("time_source")}
    if facts.get("time_sync") is not None:
        if not isinstance(facts["time_sync"], bool):
            raise FactsError("time_sync must be true or false")
        clock["time_sync"] = "synchronised" if facts["time_sync"] else "not synchronised"
    return {
        "computer": {k: v for k, v in computer.items() if v not in (None, "")},
        "clock": {k: v for k, v in clock.items() if v not in (None, "")},
    }
