"""Time indexing and full decoding of a DataFlash log.

    uv run python bench/bench_decode.py LOG [--max-seconds S]

Spec target (Data ingestion and processing): 500 MB decoded in under 60 s on a
4-core laptop, every sample decoded. With --max-seconds, exits 1 if slower.
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

from flight_insights.dataflash import read_dataflash


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--max-seconds", type=float, help="fail if the total takes longer")
    args = parser.parse_args()
    size = args.log.stat().st_size
    t0 = time.perf_counter()
    with read_dataflash(args.log) as log:
        t1 = time.perf_counter()
        tables = log.decode_all()
        t2 = time.perf_counter()
        rows = sum(t.num_rows for t in tables.values())
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(
        f"{size / 1e6:.0f} MB, {len(log.index.offsets):,} messages, {rows:,} rows, "
        f"{len(tables)} types\n"
        f"index {t1 - t0:.1f} s, decode {t2 - t1:.1f} s, total {t2 - t0:.1f} s "
        f"({size / 1e6 / (t2 - t0):.0f} MB/s), peak memory {peak_mb:.0f} MB"
    )
    if args.max_seconds is not None and t2 - t0 > args.max_seconds:
        print(f"slower than the {args.max_seconds:.0f} s target", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
