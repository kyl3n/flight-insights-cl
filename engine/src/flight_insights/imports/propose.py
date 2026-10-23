"""Which airframe each log belongs to, how sure that is, and what changed since the
airframe last flew (spec: Platforms, "Every import asks which airframe and what
changed").

- The board's keyed ID hash matches an airframe's: high confidence.
- No board ID to go on, but the tuning parameters match an airframe's last flight
  at MATCH or more: medium confidence.
- A board the fleet has not seen, whose tuning matches an airframe flown with
  another board: a decision for a person. Was the flight controller replaced, or
  is this a different aircraft? The log is held until they say.
- Nothing matches: held until a person picks an airframe or creates one.

Changes are the direct ones, read from the logs themselves: board, sensor device
IDs, CAN nodes and firmware, each with its evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from flight_insights.fingerprint.devices import HardwareChange, compare, from_json
from flight_insights.history.persist import load_snapshot
from flight_insights.history.snapshot import Snapshot, from_param_file, same_value
from flight_insights.imports.scan import Scanned
from flight_insights.knowledge import Pack
from flight_insights.platforms.detect import vehicle_of_params
from flight_insights.platforms.schema import SetupError
from flight_insights.store.db import Store

MATCH = 0.9  # share of tuning parameters with the same value
CANDIDATES = 3
Status = Literal["ready", "duplicate", "decision", "no match", "unpaired", "error"]
How = Literal["board", "parameters", "flight", "none"]


@dataclass(frozen=True)
class Candidate:
    airframe_id: str
    name: str
    score: float  # share of tuning parameters matching its last flight


@dataclass
class Proposal:
    log: str  # the scanned file's id
    status: Status
    airframe_id: str | None = None
    airframe_name: str | None = None
    confidence: Literal["high", "medium"] | None = None
    how: How = "none"
    score: float | None = None
    candidates: list[Candidate] = field(default_factory=list[Candidate])
    changes: list[HardwareChange] = field(default_factory=list[HardwareChange])
    previous_flight_id: str | None = None
    pairs_with: list[str] = field(default_factory=list[str])  # flight keys or batch flight keys
    note: str | None = None


@dataclass(frozen=True)
class LastFlight:
    flight_id: str
    snapshot_id: str | None
    board_id_hash: str | None
    hardware_json: str


def last_flight(store: Store, airframe_id: str, before_us: int | None = None) -> LastFlight | None:
    """The airframe's latest flight, or its latest that armed by `before_us` (UTC).
    Flights with no UTC time count as the oldest."""
    row = store.conn.execute(
        "SELECT id, param_snapshot_id, board_id_hash, hardware_json FROM flight "
        "WHERE airframe_id = ? AND (? IS NULL OR boot_utc_us IS NULL OR boot_utc_us + arm_us <= ?) "
        "ORDER BY boot_utc_us IS NULL, boot_utc_us DESC, arm_us DESC, created_hlc DESC LIMIT 1",
        (airframe_id, before_us, before_us),
    ).fetchone()
    return LastFlight(*row) if row else None


def tuning(snapshot: Snapshot, pack: Pack) -> dict[str, float]:
    """The tuning-class values in canonical names: what two flights of one aircraft
    share, whatever board and calibration they had."""
    dialect = snapshot.dialect(pack)
    out: dict[str, float] = {}
    for name, value in snapshot.values.items():
        if dialect.param_class(name, snapshot.values) == "tuning":
            canonical, converted = dialect.canonical(name, value)
            out[canonical] = value if converted is None else converted
    return out


def similarity(a: dict[str, float], b: dict[str, float]) -> float:
    names = a.keys() | b.keys()
    if not names:
        return 0.0
    same = sum(1 for n in names if n in a and n in b and same_value(a[n], b[n]))
    return same / len(names)


class Proposer:
    """Proposals for one batch, reading each airframe's last flight once."""

    def __init__(self, store: Store, pack: Pack) -> None:
        self.store, self.pack = store, pack
        self.airframes = [
            (r[0], r[1], r[2])
            for r in store.conn.execute(
                "SELECT id, name, board_id_hash FROM airframe ORDER BY name"
            )
        ]
        self.flown = {  # every board each airframe has flown with, current or not
            (r[0], r[1])
            for r in store.conn.execute(
                "SELECT DISTINCT airframe_id, board_id_hash FROM flight "
                "WHERE airframe_id IS NOT NULL AND board_id_hash IS NOT NULL"
            )
        }
        self._last: dict[str, LastFlight | None] = {}
        self._tuning: dict[str, dict[str, float] | None] = {}

    def last(self, airframe_id: str) -> LastFlight | None:
        if airframe_id not in self._last:
            self._last[airframe_id] = last_flight(self.store, airframe_id)
        return self._last[airframe_id]

    def last_tuning(self, airframe_id: str) -> dict[str, float] | None:
        if airframe_id not in self._tuning:
            last = self.last(airframe_id)
            snap = (
                load_snapshot(self.store, last.snapshot_id) if last and last.snapshot_id else None
            )
            self._tuning[airframe_id] = tuning(snap, self.pack) if snap else None
        return self._tuning[airframe_id]

    def ranked(self, snapshot: Snapshot) -> list[Candidate]:
        mine = tuning(snapshot, self.pack)
        out = [
            Candidate(aid, name, round(similarity(mine, theirs), 3))
            for aid, name, _ in self.airframes
            if (theirs := self.last_tuning(aid)) is not None
        ]
        return sorted(out, key=lambda c: -c.score)

    def changes(self, airframe_id: str, item: Scanned) -> tuple[str | None, list[HardwareChange]]:
        last = self.last(airframe_id)
        if last is None or item.hardware is None:
            return (last.flight_id if last else None), []
        before = from_json(json.loads(last.hardware_json or "{}"))
        if not before.firmware:  # flown before hardware was recorded
            return last.flight_id, []
        return last.flight_id, compare(
            before, item.hardware, last.board_id_hash, item.board_id_hash
        )

    def dataflash(self, item: Scanned) -> Proposal:
        p = Proposal(item.id, "ready")
        if item.flights and all(f.known_id for f in item.flights):
            p.status, p.note = "duplicate", "every flight in this log is already imported"
            return p
        if not item.flights:
            p.note = "no flight in this log: its parameters are kept as a bench snapshot"
        assert item.snapshot is not None
        ranked = self.ranked(item.snapshot)
        p.candidates = ranked[:CANDIDATES]
        by_board = next(
            (a for a in self.airframes if item.board_id_hash and a[2] == item.board_id_hash), None
        ) or next((a for a in self.airframes if (a[0], item.board_id_hash) in self.flown), None)
        best = ranked[0] if ranked and ranked[0].score >= MATCH else None
        if by_board:
            p.airframe_id, p.airframe_name, p.confidence, p.how = (
                by_board[0],
                by_board[1],
                "high",
                "board",
            )
            p.score = next((c.score for c in ranked if c.airframe_id == by_board[0]), None)
        elif best is not None:
            board_then = next(a[2] for a in self.airframes if a[0] == best.airframe_id)
            p.airframe_id, p.airframe_name, p.how, p.score = (
                best.airframe_id,
                best.name,
                "parameters",
                best.score,
            )
            if item.board_id_hash and board_then:
                p.status = "decision"
                p.note = (
                    "a board the fleet has not seen, with the tuning of "
                    f"{best.name}: flight controller replaced, or a different aircraft?"
                )
            else:
                p.confidence = "medium"
        else:
            p.status = "no match"
            p.note = "no airframe matches: pick one or create a new airframe"
        if p.airframe_id:
            p.previous_flight_id, p.changes = self.changes(p.airframe_id, item)
        return p

    def tlog(self, item: Scanned, batch: list[Scanned]) -> Proposal:
        p = Proposal(item.id, "unpaired", note="no onboard log of the same boot yet")
        for boot in item.boots:
            for other in batch:
                if other.kind == "dataflash" and other.sysid in (None, boot.sysid):
                    p.pairs_with += [
                        f.key
                        for f in other.flights
                        if f.boot_utc_us is not None
                        and abs(f.boot_utc_us - boot.boot_utc_us) <= 2_000_000
                    ]
            rows = self.store.conn.execute(
                "SELECT flight_key FROM flight WHERE boot_utc_us BETWEEN ? AND ?",
                (boot.boot_utc_us - 2_000_000, boot.boot_utc_us + 2_000_000),
            ).fetchall()
            p.pairs_with += [r[0] for r in rows if r[0] not in p.pairs_with]
        if p.pairs_with:
            p.status, p.how, p.note = "ready", "flight", None
        if item.known_source:
            p.status, p.note = "duplicate", "this file is already imported"
        return p

    def param_file(self, item: Scanned) -> Proposal:
        p = Proposal(item.id, "no match", note="no airframe's parameters match this file")
        if item.known_source:
            p.status, p.note = "duplicate", "this file is already imported"
            return p
        snapshot = file_snapshot(item, self.pack)
        if snapshot is None:
            p.note = "cannot tell from the file whether this is a copter or a plane"
            return p
        ranked = self.ranked(snapshot)
        p.candidates = ranked[:CANDIDATES]
        if ranked and ranked[0].score >= MATCH:
            best = ranked[0]
            p.status, p.how, p.confidence, p.note = "ready", "parameters", "medium", None
            p.airframe_id, p.airframe_name, p.score = best.airframe_id, best.name, best.score
        return p


def file_snapshot(item: Scanned, pack: Pack) -> Snapshot | None:
    """A parameter file's values as a snapshot. A file names no firmware, so it is
    read in the newest dialect the pack knows."""
    if item.params is None:
        return None
    try:
        vehicle = vehicle_of_params({e.name for e in item.params.entries})
    except SetupError:
        return None
    return from_param_file(item.params, vehicle, pack.versions[-1])  # type: ignore[arg-type]


def propose(store: Store, pack: Pack, batch: list[Scanned]) -> list[Proposal]:
    proposer = Proposer(store, pack)
    out: list[Proposal] = []
    for item in batch:
        if item.error:
            out.append(Proposal(item.id, "error", note=item.error))
        elif item.kind == "dataflash":
            out.append(proposer.dataflash(item))
        elif item.kind == "tlog":
            out.append(proposer.tlog(item, batch))
        else:
            out.append(proposer.param_file(item))
    return out
