"""What changed between two flights, and where each change came from (spec:
Parameter history, "Change sets and attribution").

Snapshots are compared in canonical names, so a parameter renamed by a firmware
upgrade is not a change, while a value that failed to carry over is. Each change
is labelled with its origin and the evidence for that label:

- in flight: the earlier flight's own log recorded the change while it flew
  (tuning knob, script, or a ground station during the flight);
- loaded file: a parameter file loaded between the flights sets this value;
- ground station edit: a tlog shows a station writing this value, and the
  vehicle confirming it;
- unexplained: nothing on record. These are flagged, because that is where
  regressions hide.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from flight_insights.history.snapshot import ParamChange, Snapshot, same_value
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect, ParamClass
from flight_insights.params.files import ParamFile
from flight_insights.tlog.params import ParamWrite

Origin = Literal["in flight", "loaded file", "ground station edit", "unexplained"]
DiffKind = Literal["changed", "added", "removed"]


@dataclass(frozen=True)
class ParamDiff:
    name: str  # canonical
    before: float | None  # canonical units; None: not in the earlier set
    after: float | None
    param_class: ParamClass

    @property
    def kind(self) -> DiffKind:
        if self.before is None:
            return "added"
        return "removed" if self.after is None else "changed"


@dataclass(frozen=True)
class Evidence:
    kind: Literal["log", "tlog write", "param file"]
    detail: str
    time_us: int | None = None  # log time for "log", tlog time for "tlog write"
    source: str | None = None  # the record it came from, such as a source file id


@dataclass(frozen=True)
class LabelledChange:
    diff: ParamDiff
    origin: Origin
    evidence: tuple[Evidence, ...] = field(default=())


def diff_snapshots(before: Snapshot, after: Snapshot, pack: Pack) -> list[ParamDiff]:
    """Every parameter whose value differs, in canonical names, sorted by name."""
    db, da = before.dialect(pack), after.dialect(pack)
    b = db.to_canonical(before.values).values
    a = da.to_canonical(after.values).values
    out: list[ParamDiff] = []
    for name in sorted(b.keys() | a.keys()):
        x, y = b.get(name), a.get(name)
        if x is not None and y is not None and same_value(x, y):
            continue
        # Class the parameter in the firmware that has it, with that set's values.
        dialect, values = (da, after.values) if y is not None else (db, before.values)
        native, _ = dialect.native(name)
        out.append(ParamDiff(name, x, y, dialect.param_class(native, values)))
    return out


@dataclass(frozen=True)
class LoadedFile:
    source: str  # source file id, or a name for files not yet registered
    params: ParamFile


def label_changes(
    diffs: Sequence[ParamDiff],
    dialect: Dialect,
    in_flight: Sequence[ParamChange] = (),
    writes: Sequence[ParamWrite] = (),
    files: Sequence[LoadedFile] = (),
    tlog_source: str | None = None,
) -> list[LabelledChange]:
    """Label each difference between two flights.

    `in_flight` are the earlier flight's changes after its snapshot; `writes` and
    `files` are the tlog writes and loaded files between the two flights. Their
    names are read in `dialect`, the firmware the later flight ran.
    """
    out: list[LabelledChange] = []
    for d in diffs:
        if d.after is None:
            out.append(LabelledChange(d, "unexplained"))
            continue
        flown = [c for c in in_flight if dialect.canonical(c.name)[0] == d.name]
        if flown and _same_canonical(dialect, flown[-1].name, flown[-1].new, d.after):
            c = flown[-1]
            ev = Evidence("log", f"{c.name} {c.old:g} to {c.new:g} while logging", c.time_us)
            out.append(LabelledChange(d, "in flight", (ev,)))
            continue
        evidence: list[Evidence] = []
        from_file = False
        for f in files:
            for e in f.params.entries:
                if e.component is not None and e.component[1] != 1:
                    continue
                if dialect.canonical(e.name)[0] == d.name and _same_canonical(
                    dialect, e.name, e.value, d.after
                ):
                    from_file = True
                    evidence.append(Evidence("param file", f"{e.name} = {e.text}", None, f.source))
        confirmed = False
        for w in writes:
            if dialect.canonical(w.name)[0] != d.name or w.result == "no reply":
                continue
            taken = w.reply_value if w.reply_value is not None else w.value
            if _same_canonical(dialect, w.name, taken, d.after):
                confirmed = True
                detail = f"{w.name} written by station {w.sender[0]}/{w.sender[1]}, {w.result}"
                evidence.append(Evidence("tlog write", detail, w.time_us, tlog_source))
        if from_file:
            origin: Origin = "loaded file"
        elif confirmed:
            origin = "ground station edit"
        else:
            origin = "unexplained"
        out.append(LabelledChange(d, origin, tuple(evidence)))
    return out


def _same_canonical(dialect: Dialect, name: str, value: float, canonical_value: float) -> bool:
    _, converted = dialect.canonical(name, value)
    return same_value(value if converted is None else converted, canonical_value)
