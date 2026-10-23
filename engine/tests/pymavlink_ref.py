"""pymavlink's DFReader, the reference decoder, behind a typed function."""

# pymavlink ships without type information.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path
from typing import Any

from pymavlink import DFReader


def reference(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every message pymavlink reads from a log, as (type, {column: value})."""
    reader = DFReader.DFReader_binary(str(path), zero_time_base=False)
    out: list[tuple[str, dict[str, Any]]] = []
    while (m := reader.recv_msg()) is not None:
        out.append((m.get_type(), {c: getattr(m, c) for c in m.get_fieldnames()}))
    return out
