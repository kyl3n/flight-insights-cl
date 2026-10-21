"""The Phase 0 gate: the engine must decode every log exactly as pymavlink does.

Each log is read by both decoders in step, message by message, without holding
either in memory. The comparison is bit-exact: the same message types in the
same order, the same columns, and the same values, floats compared by their
bits. See docs/SPEC.md, "Testing and validation".

The report can be shared: logs are named by a hash of their content, never by
file name (names often say where a flight took place), and it holds message
type names, column names, counts and positions in the file, never values.
"""

from __future__ import annotations

import array
import contextlib
import hashlib
import io
import json
import multiprocessing
import struct
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from flight_insights import __version__
from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.compat import pymavlink_messages
from flight_insights.dataflash.reference import pymavlink_version, reference_messages

Status = Literal["match", "mismatch", "engine_failed", "reference_failed"]
MAX_LISTED = 20  # mismatches listed per log; all are counted


def same(a: Any, b: Any) -> bool:
    """Equal, and for floats equal bit for bit (so -0.0 and NaN payloads count)."""
    if isinstance(a, array.array):
        a = a.tolist()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(a, float) or isinstance(b, float):
        return (
            isinstance(a, float)
            and isinstance(b, float)
            and struct.pack("<d", a) == struct.pack("<d", b)
        )
    return type(a) is type(b) and a == b


@dataclass
class Mismatch:
    message: int  # position in the file's message sequence
    type: str
    column: str | None
    problem: str


@dataclass
class LogResult:
    id: str  # first 16 hex digits of the file's SHA-256
    bytes: int
    status: Status
    messages: int  # messages compared
    types: dict[str, int] = field(default_factory=dict[str, int])
    mismatches: int = 0
    first_mismatches: list[Mismatch] = field(default_factory=list[Mismatch])
    error: str | None = None  # exception type, when a decoder failed


def log_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _diverge(
    ref: Iterator[tuple[str, dict[str, Any]]],
    ours: Iterator[tuple[str, dict[str, Any]]],
    result: LogResult,
    types: Counter[str],
) -> None:
    def note(i: int, typ: str, column: str | None, problem: str) -> None:
        result.mismatches += 1
        if len(result.first_mismatches) < MAX_LISTED:
            result.first_mismatches.append(Mismatch(i, typ, column, problem))

    i = 0
    for rt, rv in ref:
        theirs = next(ours, None)
        if theirs is None:
            note(i, rt, None, "missing from the engine's output")
            result.mismatches += sum(1 for _ in ref)
            return
        ot, ov = theirs
        types[rt] += 1
        if rt != ot:
            note(i, rt, None, f"the engine has {ot} here; the sequences diverge")
            return
        if list(rv) != list(ov):
            note(i, rt, None, "columns differ")
        else:
            for col, value in rv.items():
                if not same(value, ov[col]):
                    note(i, rt, col, "value differs")
        i += 1
        result.messages = i
    extra = sum(1 for _ in ours)
    if extra:
        note(i, "-", None, f"{extra} message(s) more than pymavlink")
        result.mismatches += extra - 1


def compare_log(path: Path) -> LogResult:
    """Compare one log. Never raises: decoder failures become the result's status."""
    result = LogResult(id=log_id(path), bytes=path.stat().st_size, status="match", messages=0)
    types: Counter[str] = Counter()
    # pymavlink prints diagnostics to stdout; keep them out of the gate's own output.
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            log = read_dataflash(path)
        except Exception as e:
            result.status, result.error = "engine_failed", type(e).__name__
            return result
        with log:
            ref = reference_messages(path)
            try:
                _diverge(ref, iter(pymavlink_messages(log)), result, types)
            except Exception as e:
                # Tell apart which decoder raised by probing the reference on its own.
                result.status, result.error = _blame(path, e)
                result.types = dict(types)
                return result
    result.types = dict(types)
    if result.mismatches:
        result.status = "mismatch"
    return result


def _blame(path: Path, error: Exception) -> tuple[Status, str]:
    try:
        for _ in reference_messages(path):
            pass
    except Exception as e:
        return "reference_failed", type(e).__name__
    return "engine_failed", type(error).__name__


def find_logs(corpus: Path) -> list[Path]:
    return sorted(p for p in corpus.rglob("*") if p.is_file() and p.suffix.lower() == ".bin")


def run(paths: Sequence[Path], jobs: int = 1) -> list[LogResult]:
    if jobs <= 1:
        return [compare_log(p) for p in paths]
    # spawn, as on Windows: forking a multi-threaded process can deadlock.
    with ProcessPoolExecutor(
        max_workers=jobs, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        return list(pool.map(compare_log, paths))


def report(results: Iterable[LogResult]) -> dict[str, Any]:
    results = list(results)
    return {
        "gate": "DataFlash decode matches pymavlink",
        "engine_version": __version__,
        "pymavlink_version": pymavlink_version(),
        "date": datetime.now(UTC).date().isoformat(),
        "passed": all(r.status == "match" for r in results),
        "summary": dict(Counter(r.status for r in results)),
        "logs": [asdict(r) for r in results],
    }


def write_report(data: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
