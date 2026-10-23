"""The filter model checked over a folder of logs: the Phase 1 gate's filter
prediction check (`fi-engine filter-check`).

Each log is decoded, its steady hover found, and the filter model run on its own
settings against the post-filter gyro data it logged. The report names logs by a
hash of their content and holds only errors in dB and the filter setup, never
samples, positions or file names, so it can be shared.
"""

from __future__ import annotations

import multiprocessing
import secrets
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np

from flight_insights import __version__
from flight_insights.dataflash.log import read_dataflash
from flight_insights.filters.check import check
from flight_insights.fingerprint import fingerprint
from flight_insights.gate import log_id
from flight_insights.history import from_log
from flight_insights.knowledge import load_pack

Status = Literal["checked", "no post-filter data", "no pre-filter data", "no hover", "error"]
TABLES = ("GYR", "ESC", "MOTB", "FCN", "FCNS", "RPM")


@dataclass
class LogCheck:
    id: str  # first 16 hex digits of the file's SHA-256
    status: Status
    spectrum_error_db: float | None = None
    control_band_error_db: float | None = None
    rate_hz: float | None = None
    seconds: float | None = None
    notch_modes: list[int] = field(default_factory=list[int])
    error: str | None = None


def check_log(path: Path) -> LogCheck:
    ident = log_id(path)
    try:
        pack = load_pack()
        with read_dataflash(path) as df:
            snapshot = from_log(df)
            params = snapshot.dialect(pack).to_canonical(snapshot.values).values
            fp = fingerprint(df, pack, secrets.token_bytes(32))
            tables = {n: df.table(n) for n in TABLES if n in df.message_types}
        spans = [(w.start_us, w.end_us) for w in fp.hover_windows]
        if not spans:
            return LogCheck(ident, "no hover")
        result = check(params, tables, spans, fp.imu_count.value, fp.board.family)
        if result is None:
            return LogCheck(ident, "no pre-filter data")
        modes = [s.notch.p.mode for s in result.filters.sets]
        error = result.spectrum_error_db()
        return LogCheck(
            ident,
            "checked" if error is not None else "no post-filter data",
            error,
            result.control_band_error_db(),
            round(result.rate_hz, 1),
            round(result.prediction.seconds, 1),
            modes,
        )
    except Exception as e:  # a log that cannot be checked is reported, not fatal
        return LogCheck(ident, "error", error=type(e).__name__)


def find_logs(corpus: Path) -> list[Path]:
    return sorted(p for p in corpus.rglob("*") if p.is_file() and p.suffix.lower() == ".bin")


def run(paths: Sequence[Path], jobs: int = 1) -> list[LogCheck]:
    if jobs <= 1:
        return [check_log(p) for p in paths]
    with ProcessPoolExecutor(
        max_workers=jobs, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        return list(pool.map(check_log, paths))


def report(results: Iterable[LogCheck], target_db: float | None = None) -> dict[str, Any]:
    """Aggregate errors; with a target, whether every checked log meets it."""
    results = list(results)
    errors = np.array([r.spectrum_error_db for r in results if r.spectrum_error_db is not None])
    stats = (
        {
            "median_db": round(float(np.median(errors)), 3),
            "p90_db": round(float(np.percentile(errors, 90)), 3),
            "max_db": round(float(errors.max()), 3),
        }
        if len(errors)
        else {}
    )
    return {
        "gate": "filter model reproduces the logged post-filter gyro spectrum",
        "engine_version": __version__,
        "date": datetime.now(UTC).date().isoformat(),
        "checked": len(errors),
        "target_db": target_db,
        "passed": (
            bool(len(errors)) and bool((errors <= target_db).all())
            if target_db is not None
            else None
        ),
        "spectrum_error": stats,
        "summary": dict(Counter(r.status for r in results)),
        "logs": [asdict(r) for r in results],
    }
