"""pymavlink's DFReader, the reference decoder for the Phase 0 gate, behind a typed function."""

# pymavlink ships without type information.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pymavlink import DFReader


def reference_messages(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every message pymavlink reads from a log, in order, as (type, {column: value})."""
    reader = DFReader.DFReader_binary(str(path), zero_time_base=False)
    try:
        while (m := reader.recv_msg()) is not None:
            yield m.get_type(), {c: getattr(m, c) for c in m.get_fieldnames()}
    finally:
        reader.data_map.close()
        reader.filehandle.close()


def pymavlink_version() -> str:
    from importlib.metadata import version

    return version("pymavlink")
