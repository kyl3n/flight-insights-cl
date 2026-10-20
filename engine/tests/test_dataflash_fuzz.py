"""Property tests: logs are untrusted input (spec: Security and deployment).

1. Whatever the bytes, the decoder returns or rejects cleanly: no crash, no hang.
2. For damage pymavlink is known to survive (corrupted message contents, junk
   between messages, a cut-off end), the engine still decodes exactly as it does.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

from df_builder import LogBuilder
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flight_insights import gate
from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.compat import pymavlink_messages
from flight_insights.dataflash.index import index_log


def base_log() -> tuple[bytes, list[tuple[int, int]]]:
    """A small log with most format characters, and the (start, end) of each data body."""
    log = LogBuilder()
    log.fmt(0x90, "GPS", "QBLLfcM", "TimeUS,I,Lat,Lng,Alt,Spd,Mode")
    log.units("GPS", "s#DUm--", "F-GG0--")
    log.fmt(0x91, "IMU", "QBfffhHiIqd", "TimeUS,I,GyrX,GyrY,GyrZ,A,B,C,D,E,F")
    log.fmt(0x92, "MSG", "QZ", "TimeUS,Message")
    log.fmt(0x93, "PARM", "QNff", "TimeUS,Name,Value,Default")
    log.fmt(0x94, "ARR", "QaneE", "TimeUS,V,N,X,Y")
    bodies: list[tuple[int, int]] = []

    def add(name: str, *values: object) -> None:
        start = len(log.data) + 3
        log.write(name, *values)
        bodies.append((start, len(log.data)))

    for t in range(12):
        add("GPS", t, t % 2, -353632621 + t, 1491652374, 584.5, -t, t)
        add("IMU", t, t % 3, 0.1 * t, -0.2, 9.8, -t, t, -(t**3), t**3, -(2**40), 1e-3 * t)
        if t % 4 == 0:
            add("MSG", t, f"message {t}".encode())
            add("PARM", t, b"ATC_RAT_RLL_P", 0.135, 0.135)
            add("ARR", t, list(range(-16, 16)), b"abc", -12345, 98765)
    return log.bytes(), bodies


BASE, BODIES = base_log()
# Examples per test come from the Hypothesis profile (tests/conftest.py).
FUZZ = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def decode_everything(data: bytes) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fuzz.bin"
        path.write_bytes(data)
        with read_dataflash(path) as log:
            n = sum(1 for _ in pymavlink_messages(log))
            for name in log.message_types:
                # ValueError: a type redefined with different columns, documented, not a crash
                with contextlib.suppress(ValueError):
                    log.table(name)
        return n


def matches_pymavlink(data: bytes) -> gate.LogResult:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fuzz.bin"
        path.write_bytes(data)
        return gate.compare_log(path)


@FUZZ
@given(st.binary(max_size=4096))
def test_random_bytes_never_crash_the_decoder(data: bytes) -> None:
    index_log(data)
    decode_everything(data)


@FUZZ
@given(
    st.lists(st.tuples(st.integers(0, len(BASE) - 1), st.integers(0, 255)), max_size=40),
    st.integers(0, len(BASE)),
)
def test_any_damage_to_a_real_log_never_crashes_the_decoder(
    flips: list[tuple[int, int]], cut: int
) -> None:
    data = bytearray(BASE)
    for at, value in flips:
        data[at] = value
    decode_everything(bytes(data[:cut]))


@st.composite
def body_edits(draw: st.DrawFn) -> list[tuple[int, int]]:
    """(position, new byte) inside message bodies, so every message keeps its framing."""
    edits: list[tuple[int, int]] = []
    for _ in range(draw(st.integers(0, 40))):
        start, end = draw(st.sampled_from(BODIES))
        edits.append((draw(st.integers(start, end - 1)), draw(st.integers(0, 255))))
    return edits


@FUZZ
@given(
    body_edits(),
    st.lists(
        st.tuples(
            st.integers(0, len(BODIES) - 1),
            st.binary(min_size=1, max_size=40).filter(lambda b: 0xA3 not in b),
        ),
        max_size=5,
    ),
    st.integers(len(BASE) // 2, len(BASE)),
)
def test_damage_pymavlink_survives_decodes_exactly_as_pymavlink(
    edits: list[tuple[int, int]],
    junk: list[tuple[int, bytes]],
    cut: int,
) -> None:
    data = bytearray(BASE)
    for at, value in edits:  # corrupt values inside message bodies
        data[at] = value
    for i, blob in sorted(junk, reverse=True):  # junk between messages, back to front
        at = BODIES[i][1]
        data[at:at] = blob
    result = matches_pymavlink(bytes(data[:cut]))
    assert result.status == "match", result.first_mismatches
