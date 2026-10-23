"""A decoded DataFlash log."""

from __future__ import annotations

import mmap
from collections.abc import Iterator
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import numpy as np
import pyarrow as pa

from flight_insights.dataflash.decode import BLOCK_ROWS, decode_rows, to_table
from flight_insights.dataflash.formats import FMT_FORMAT, DfFormat, text
from flight_insights.dataflash.index import LogIndex, index_log


class DataflashLog:
    """A DataFlash log read from its own FMT, FMTU, UNIT and MULT messages.

    Use as a context manager, or call close(): the file stays memory-mapped
    while tables are decoded on demand.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("rb")
        size = path.stat().st_size
        self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ) if size else None
        self._bytes = (
            np.frombuffer(self._map, dtype=np.uint8) if self._map else np.empty(0, np.uint8)
        )
        self.index: LogIndex = index_log(self._map if self._map else b"")
        self._rows: dict[int, np.ndarray[tuple[int], np.dtype[np.void]]] = {}
        self._apply_units()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self._rows.clear()
        self._bytes = np.empty(0, np.uint8)
        if self._map is not None:
            self._map.close()
        self._file.close()

    # --- formats and units ---------------------------------------------------------

    def _apply_units(self) -> None:
        """Attach FMTU units to every format of the type they name, wherever FMTU sits.

        UNIT, MULT and FMTU are used only when their columns have the kinds the
        format gives them (ids as integers, labels as strings, multipliers as
        numbers); a log that defines them otherwise simply has no units.
        """
        units: dict[str, str] = {}
        mults: dict[str, float] = {}
        fmtu: dict[int, tuple[str, str]] = {}
        for fid, fmt in enumerate(self.index.formats):
            if fmt.name == "UNIT" and _kinds(fmt, Id="iu", Label="S"):
                for r in self.rows(fid):
                    units[chr(int(r["Id"]) & 0xFF)] = text(bytes(r["Label"]))
            elif fmt.name == "MULT" and _kinds(fmt, Id="iu", Mult="iuf"):
                for r in self.rows(fid):
                    mults[chr(int(r["Id"]) & 0xFF)] = float(r["Mult"])
            elif fmt.name == "FMTU" and _kinds(fmt, FmtType="iu", UnitIds="S", MultIds="S"):
                for r in self.rows(fid):
                    fmtu[int(r["FmtType"])] = (
                        text(bytes(r["UnitIds"])),
                        text(bytes(r["MultIds"])),
                    )
        for fid, fmt in enumerate(self.index.formats):
            if fmt.type in fmtu:
                if fmt is FMT_FORMAT:  # shared by every log: units go on this log's own copy
                    fmt = self.index.formats[fid] = replace(
                        fmt, units=list(fmt.units), mults=list(fmt.mults)
                    )
                fmt.apply_units(*fmtu[fmt.type], units, mults)

    @cached_property
    def message_types(self) -> list[str]:
        """Names of the message types present, in order of first appearance."""
        seen: dict[str, None] = {}
        firsts = np.unique(self.index.format_ids, return_index=True)
        for fid in firsts[0][np.argsort(firsts[1])]:
            seen.setdefault(self.index.formats[int(fid)].name, None)
        return list(seen)

    def formats_named(self, name: str) -> list[int]:
        """Format ids with this name: more than one if the log redefined the type."""
        return [i for i, f in enumerate(self.index.formats) if f.name == name]

    def format(self, name: str) -> DfFormat:
        ids = [i for i in self.formats_named(name) if self.count_format(i)]
        if not ids:
            raise KeyError(name)
        return self.index.formats[ids[-1]]

    # --- messages --------------------------------------------------------------------

    @cached_property
    def _offsets_by_format(self) -> list[np.ndarray[tuple[int], np.dtype[np.int64]]]:
        """Each format's message offsets in file order, grouped in one pass over the index."""
        ids = self.index.format_ids
        order = np.argsort(ids, kind="stable")
        counts = np.bincount(ids, minlength=len(self.index.formats))
        return np.split(self.index.offsets[order], np.cumsum(counts)[:-1])

    def offsets(self, fid: int) -> np.ndarray[tuple[int], np.dtype[np.int64]]:
        return self._offsets_by_format[fid]

    def count_format(self, fid: int) -> int:
        return len(self._offsets_by_format[fid])

    def rows(self, fid: int) -> np.ndarray[tuple[int], np.dtype[np.void]]:
        """Stored values of every message written with format `fid`, in file order."""
        if fid not in self._rows:
            self._rows[fid] = decode_rows(self._bytes, self.offsets(fid), self.index.formats[fid])
        return self._rows[fid]

    def field(self, fid: int, name: str) -> np.ndarray[tuple[int], np.dtype[Any]]:
        """One field's stored values for format `fid`, in file order, gathered on its own
        so whole-log passes (timestamps of every message) need not decode every row."""
        if fid in self._rows:
            return self._rows[fid][name]
        fmt = self.index.formats[fid]
        fields = cast(dict[str, tuple[Any, ...]], dict(fmt.dtype.fields or {}))
        if name not in fields:
            raise KeyError(f"{fmt.name} has no field {name}")
        dtype: np.dtype[Any] = fields[name][0]
        offset = int(fields[name][1])
        offsets = self.offsets(fid)
        out = np.empty(len(offsets), dtype=dtype)
        raw = out.view(np.uint8).reshape(len(offsets), dtype.itemsize)
        body = np.arange(3 + offset, 3 + offset + dtype.itemsize, dtype=np.int64)
        for start in range(0, len(offsets), BLOCK_ROWS):
            block = offsets[start : start + BLOCK_ROWS]
            raw[start : start + len(block)] = self._bytes[block[:, None] + body]
        return out

    def table(self, name: str) -> pa.Table:
        """Every message of a type as an Arrow table, in file order.

        A type redefined part-way through a log keeps one table only if every
        definition has the same columns and types; otherwise this raises ValueError.
        """
        ids = [i for i in self.formats_named(name) if self.count_format(i)]
        if not ids:
            raise KeyError(name)
        tables = [to_table(self.rows(i), self.index.formats[i]) for i in ids]
        if len(tables) == 1:
            return tables[0]
        if any(not t.schema.equals(tables[0].schema) for t in tables):
            raise ValueError(f"{name} is defined with different columns or types in this log")
        order = np.argsort(
            np.concatenate([self.offsets(i) for i in ids]),
            kind="stable",
        )
        return pa.concat_tables(tables).take(order)

    def decode_all(self) -> dict[str, pa.Table]:
        """Every type's table. Types redefined with different columns are left out;
        read those one definition at a time with rows() or iter_batches().

        Single-threaded on purpose: decoding 500 MB takes about 17 s on one core,
        and threads measured slower, as the Arrow and string work holds the GIL.
        """
        tables = {n: self._table_or_none(n) for n in self.message_types}
        return {n: t for n, t in tables.items() if t is not None}

    def _table_or_none(self, name: str) -> pa.Table | None:
        try:
            return self.table(name)
        except ValueError:
            return None  # redefined with different columns: read each format with rows()

    def iter_batches(self, name: str, rows: int = 1 << 16) -> Iterator[pa.RecordBatch]:
        """A type's messages in file order, `rows` at a time, never all in memory at once.

        For a type redefined part-way through a log, batches follow each definition
        in turn, so their schemas can differ.
        """
        ids = [i for i in self.formats_named(name) if self.count_format(i)]
        if not ids:
            raise KeyError(name)
        for fid in ids:
            offsets = self.offsets(fid)
            fmt = self.index.formats[fid]
            for start in range(0, len(offsets), rows):
                chunk = decode_rows(self._bytes, offsets[start : start + rows], fmt)
                yield from to_table(chunk, fmt).to_batches()

    def iter_selected(
        self, fid: int, positions: np.ndarray[tuple[int], np.dtype[np.intp]], rows: int = 1 << 16
    ) -> Iterator[pa.RecordBatch]:
        """Chosen messages of one format (by their positions in it), `rows` at a time."""
        offsets = self.offsets(fid)[positions]
        fmt = self.index.formats[fid]
        for start in range(0, len(offsets), rows):
            chunk = decode_rows(self._bytes, offsets[start : start + rows], fmt)
            yield from to_table(chunk, fmt).to_batches()

    def instances(self, name: str) -> dict[int, pa.Table]:
        """A type's messages split by its instance column (IMU, ESC, battery...)."""
        table = self.table(name)
        column = self.format(name).instance_column
        if column is None:
            return {0: table}
        values: np.ndarray[tuple[int], np.dtype[np.integer]] = table.column(column).to_numpy()
        return {int(v): table.filter(values == v) for v in np.unique(values)}


def _kinds(fmt: DfFormat, **expected: str) -> bool:
    """True if each named column exists with a numpy kind among those given ("iu", "S"...)."""
    fields: dict[str, Any] = {str(k): v for k, v in (fmt.dtype.fields or {}).items()}
    return all(
        name in fields and fields[name][0].kind in kinds and fields[name][0].shape == ()
        for name, kinds in expected.items()
    )


def read_dataflash(path: Path | str) -> DataflashLog:
    return DataflashLog(Path(path))
