"""Command-line entry point: `fi-engine`."""

import argparse
from collections.abc import Sequence

from flight_insights import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fi-engine",
        description="Flight Insights analysis engine",
    )
    parser.add_argument("--version", action="version", version=f"fi-engine {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
