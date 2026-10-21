"""Find every message in a DataFlash log.

The walk follows pymavlink's DFReader, the reference decoder, so both see the
same messages:
- every format the file defines is known from the start, as pymavlink's
  first passes (index and clock) make it, so messages that come before their
  FMT still decode;
- a message starts at the 2-byte header followed by a defined type; any other
  byte is skipped on its own and parsing resyncs;
- FMT messages take effect again as they are read, so a type can be redefined
  part-way through a log;
- a message cut off by the end of the file ends the walk;
- a message whose format cannot be unpacked (its FMT length disagrees with its
  format characters), or an FMT message that cannot be turned into a format, is
  not a message: parsing resumes after its 3-byte header. Within the last 528
  bytes such a message ends the walk instead.

Two cases deliberately differ from pymavlink 2.4.50, where it is wrong:
- a type redefined with a different layout: pymavlink keeps unpacking it with
  the first layout it saw, which fails, and stops reading;
- an FMT with a format character it does not know: pymavlink raises and reads
  nothing. Here that FMT defines nothing, and parsing resyncs.
ArduPilot does not write either within one log.
"""

from __future__ import annotations

from array import array
from collections.abc import Buffer
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from flight_insights.dataflash.formats import (
    FMT_FORMAT,
    FMT_STRUCT,
    FMT_TYPE,
    HEAD,
    DfFormat,
    FormatError,
    text,
)

# pymavlink stops at an undecodable message this close to the end: APM logs often end in garbage.
TAIL_GARBAGE = 528

Stop = Literal["end", "truncated", "bad-tail"]


@dataclass
class LogIndex:
    formats: list[DfFormat]  # every format the log defined, in order; position = format id
    offsets: np.ndarray[tuple[int], np.dtype[np.int64]]  # header offset of each message
    format_ids: np.ndarray[tuple[int], np.dtype[np.uint16]]  # format of each message
    skipped: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])  # (start, len)
    rejected: list[int] = field(default_factory=list[int])  # headers of undecodable messages
    stop: Stop = "end"
    stop_offset: int = 0

    @property
    def skipped_bytes(self) -> int:
        return sum(n for _, n in self.skipped)


def _parse_fmt(buf: memoryview, o: int, length: int) -> DfFormat | None:
    """The format an FMT message at `o` defines, or None if it cannot be read. A log
    may redefine FMT itself with a shorter body; such a message, or one cut off at
    the end of the file, is not read past its own end."""
    if length < 3 + FMT_STRUCT.size or len(buf) - (o + 3) < FMT_STRUCT.size:
        return None
    t, length, name, f, cols = FMT_STRUCT.unpack_from(buf, o + 3)
    columns = text(cols).split(",") if text(cols) else []
    try:
        return DfFormat.build(t, text(name), length, text(f), columns)
    except FormatError:
        return None


def _walk(buf: memoryview, active: dict[int, DfFormat]) -> LogIndex:
    """One pass over the file with `active` as the formats known at its start."""
    n = len(buf)
    head0, head1 = HEAD
    formats: list[DfFormat] = []
    ids: dict[int, int] = {}
    offsets = array("q")
    format_ids = array("H")
    skipped: list[tuple[int, int]] = []
    rejected: list[int] = []
    skip_start = -1
    stop: Stop = "end"
    o = 0
    while n - o >= 3:
        fmt = active.get(buf[o + 2]) if buf[o] == head0 and buf[o + 1] == head1 else None
        if fmt is None:
            if skip_start < 0:
                skip_start = o
            o += 1
            continue
        if skip_start >= 0:
            skipped.append((skip_start, o - skip_start))
            skip_start = -1
        remaining = n - (o + 3)
        if remaining < fmt.body_size:
            stop = "truncated"
            break
        if not fmt.decodable:
            if remaining < TAIL_GARBAGE:
                stop = "bad-tail"
                break
            rejected.append(o)
            o += 3
            continue
        if fmt.name == "FMT":
            new = _parse_fmt(buf, o, fmt.length)
            if new is None:
                rejected.append(o)
                o += 3
                continue
            active[new.type] = new
        fid = ids.get(id(fmt))
        if fid is None:
            fid = ids[id(fmt)] = len(formats)
            formats.append(fmt)
        offsets.append(o)
        format_ids.append(fid)
        o += fmt.length
    if skip_start >= 0:
        skipped.append((skip_start, o - skip_start))
    return LogIndex(
        formats=formats,
        offsets=np.frombuffer(offsets, dtype=np.int64).copy(),
        format_ids=np.frombuffer(format_ids, dtype=np.uint16).copy(),
        skipped=skipped,
        rejected=rejected,
        stop=stop,
        stop_offset=o,
    )


def index_log(data: Buffer) -> LogIndex:
    buf = memoryview(data).cast("B")
    # First pass: learn every format the file defines (pymavlink knows them all
    # before it reads, from its pre-scan and clock pass). Second pass: the index.
    learned: dict[int, DfFormat] = {FMT_TYPE: FMT_FORMAT}
    _walk(buf, learned)
    return _walk(buf, dict(learned))
