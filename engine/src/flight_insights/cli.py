"""Command-line entry point: `fi-engine`."""

import argparse
import json
import logging
import multiprocessing
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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
    analyze = commands.add_parser(
        "analyze",
        help="validate, segment and fingerprint one .bin log",
        description=(
            "Decode a .bin log and report its validation, flights, steady hover, parameter "
            "set and vehicle fingerprint. The output holds no positions."
        ),
    )
    analyze.add_argument("log", type=Path)
    analyze.add_argument("--json", action="store_true", help="print the full result as JSON")

    decode = commands.add_parser("decode", help="what a .bin log holds: message types and counts")
    decode.add_argument("log", type=Path)

    commands.add_parser(
        "rpc",
        help="serve JSON-RPC 2.0 on stdin and stdout (how the desktop app runs the engine)",
    )
    return parser


def _print_json(data: object) -> None:
    from flight_insights.rpc.server import jsonable

    print(json.dumps(jsonable(data), indent=2, allow_nan=False))


def _analyze(args: argparse.Namespace) -> int:
    from flight_insights.analysis import analyze_log
    from flight_insights.dataflash import read_dataflash
    from flight_insights.knowledge import load_pack

    path: Path = args.log
    with read_dataflash(path) as df:
        result = analyze_log(path, df, load_pack(), None)
    if args.json:
        _print_json(result)
        return 0
    fp: dict[str, Any] = result["fingerprint"]
    params: dict[str, Any] = result["parameters"]
    frame: dict[str, Any] = fp["frame"]["value"] or {}
    print(f"{path.name}: {params['vehicle']} {params['firmware']}, {frame.get('frame_class')}")
    print(f"  parameters: {params['count']}, set {params['set_id'][:16]}")
    for f in result["flights"]:
        modes = ", ".join(m["name"] for m in f["modes"])
        print(f"  flight {f['number']}: {f['duration_s']:.1f} s ({modes})")
    print(f"  steady hover: {fp['hover_s']:.1f} s")
    for i in result["validation"]["issues"]:
        print(f"  {i['kind']}: {i['detail']}")
    best = result["archetypes"]["ranked"][:1]
    if best:
        b = best[0]
        score = f"score {b['score']:.2f}, confidence {b['confidence']:.2f}"
        print(f"  archetype: {b['archetype']} ({score})")
    return 0


def _decode(args: argparse.Namespace) -> int:
    from flight_insights.analysis import summary
    from flight_insights.dataflash import read_dataflash

    with read_dataflash(args.log) as df:
        _print_json(summary(df))
    return 0


def _rpc() -> int:
    from flight_insights.rpc import build_server, run_stdio

    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    return run_stdio(build_server)


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
    if args.command == "analyze":
        return _analyze(args)
    if args.command == "decode":
        return _decode(args)
    if args.command == "rpc":
        return _rpc()
    build_parser().print_help()
    return 0
