"""Build a large DataFlash log for benchmarks by replaying a real log's messages.

The header (formats, units, parameters: everything before the first data message
that is not FMT, FMTU, UNIT, MULT or PARM) is kept once; the rest is repeated,
with every TimeUS column shifted forward each time round, until the target size.
The source should be a scrubbed fixture, e.g. tests/fixtures/sitl/4.7.0/copter-quad-rawimu.bin.

    uv run python bench/make_large_log.py SOURCE OUT --megabytes 500
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from flight_insights.dataflash import read_dataflash

SETUP = {"FMT", "FMTU", "UNIT", "MULT", "PARM"}


def build(source: Path, out: Path, megabytes: int) -> int:
    data = source.read_bytes()
    with read_dataflash(source) as log:
        idx = log.index
        names = [idx.formats[int(f)].name for f in idx.format_ids]
        first = next(i for i, n in enumerate(names) if n not in SETUP)
        head_end = int(idx.offsets[first])
        span_end = int(idx.offsets[-1] + idx.formats[int(idx.format_ids[-1])].length)
        body = np.frombuffer(data[head_end:span_end], dtype=np.uint8).copy()
        # Where each TimeUS (a little-endian uint64 right after the header) sits in the body.
        stamps: list[int] = []
        for o, f in zip(idx.offsets[first:], idx.format_ids[first:], strict=True):
            fmt = idx.formats[int(f)]
            if fmt.columns[:1] == ["TimeUS"] and fmt.format[:1] == "Q":
                stamps.append(int(o) - head_end + 3)
        duration = 0
        if stamps:
            t = np.array([int.from_bytes(body[s : s + 8].tobytes(), "little") for s in stamps])
            duration = int(t.max() - t.min()) + 1000
    target = megabytes * 1_000_000
    stamp_at = np.array(stamps, dtype=np.int64)
    with out.open("wb") as f:
        f.write(data[:head_end])
        written, rounds = head_end, 0
        while written < target:
            chunk = body.copy()
            if len(stamp_at) and rounds:
                view = chunk[stamp_at[:, None] + np.arange(8)].copy().view("<u8").reshape(-1)
                view += np.uint64(duration * rounds)
                chunk[stamp_at[:, None] + np.arange(8)] = view.view(np.uint8).reshape(-1, 8)
            f.write(chunk.tobytes())
            written += len(chunk)
            rounds += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--megabytes", type=int, default=500)
    args = parser.parse_args()
    size = build(args.source, args.out, args.megabytes)
    print(f"wrote {size / 1e6:.0f} MB to {args.out}")


if __name__ == "__main__":
    main()
