"""Command-line entry point: `fi-engine`."""

import argparse
import multiprocessing
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from flight_insights import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fi-engine",
        description="Flight Insights analysis engine",
    )
    parser.add_argument("--version", action="version", version=f"fi-engine {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    gate = commands.add_parser(
        "gate",
        help="check that every .bin log in a folder decodes exactly as pymavlink decodes it",
        description=(
            "The Phase 0 gate. Compares the engine's decoding of every .bin log under "
            "CORPUS with pymavlink's, message by message. The report names logs by a hash "
            "of their content and holds no values or file names, so it can be shared."
        ),
    )
    gate.add_argument("--corpus", type=Path, required=True, help="folder searched for .bin logs")
    gate.add_argument("--report", type=Path, help="write the shareable JSON report here")
    gate.add_argument(
        "--jobs", type=int, default=os.cpu_count() or 1, help="logs compared in parallel"
    )
    return parser


def _gate(args: argparse.Namespace) -> int:
    from flight_insights.gate import find_logs, report, run, write_report

    corpus: Path = args.corpus
    paths = find_logs(corpus)
    if not paths:
        print(f"no .bin logs under {corpus}", file=sys.stderr)
        return 2
    results = run(paths, args.jobs)
    # File names stay on this screen; the report file uses content hashes only.
    for path, r in zip(paths, results, strict=True):
        detail = f"{r.mismatches} mismatch(es)" if r.mismatches else (r.error or "")
        print(f"{r.status:<17}{r.id}  {r.messages:>10} msgs  {detail:<24} {path}")
    data = report(results)
    if args.report:
        write_report(data, args.report)
    counts = ", ".join(f"{n} {status}" for status, n in sorted(data["summary"].items()))
    print(f"\n{'PASSED' if data['passed'] else 'FAILED'}: {len(results)} log(s): {counts}")
    return 0 if data["passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()  # worker processes in a frozen (PyInstaller) build
    args = build_parser().parse_args(argv)
    if args.command == "gate":
        return _gate(args)
    return 0
