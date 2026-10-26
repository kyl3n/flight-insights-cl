#!/usr/bin/env python3
"""Rehearse the app's procedures in ArduPilot SITL (spec: Testing and validation,
"Procedure rehearsal").

For each rehearsal in rehearsals.py:

1. Start the simulator with the weaker setup and fly the flight that shows it.
2. Import the log into a fresh store, plan it, and export the plan's files: each
   step, the revert file, the undo file and the test card. The undo file must hold
   every value the aircraft had.
3. Carry out the session's procedure exactly as written: load each step's file,
   refresh or reboot where it says, and check that every parameter a file names is
   listed when it is loaded, so that no switch, refresh or reboot is missing.
4. Fly the test flight the procedure asks for, import its log, and plan it. That
   grades the plan before: every parameter change must be confirmed, with each of its
   checks passed. The new plan must offer to take that flight's test settings out,
   unless its own test flight needs them again.
5. Repeat from 2 with the new plan, as a person works through the stages (a problem
   in an earlier stage, such as logging dropouts, is fixed first), until every finding
   the rehearsal plants has been on offer, or for at most `rounds` plans.
6. Last, the revert file must take the test-flight settings out, and the undo file
   must put back every value the last plan started from.

The simulator does not reproduce a real aircraft's vibration, so a rehearsal proves
the procedure (its order, its files, its gates and reboots, its checks, its revert
and undo), never that its values suit a real aircraft.

Safety: this tool only ever talks to a simulator it started itself on this computer.
Before anything is written, every connection is checked: the address must be a
loopback address, and the vehicle must have the simulator's own SIM_ parameters.
Anything else is refused. Nothing here can write to a real aircraft.

The simulator's logs hold its (public, synthetic) home position, so they stay in the
working folder, which is deleted afterwards unless --work names one. The output
folder gets only the plan's files, the test cards and the report.

Usage (from the repository root, with the engine's environment):
  uv run --project engine python tools/procedures/rehearse.py --ardupilot DIR \\
      --version 4.7.0 --out DIR [REHEARSAL ...]
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from pymavlink import mavutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tools" / "fixtures" / "sitl"))
sys.path.insert(0, str(HERE))

from flight_insights.analyzers.registry import Registry  # noqa: E402
from flight_insights.imports import Decision, commit, propose, scan  # noqa: E402
from flight_insights.knowledge import Pack, load_pack  # noqa: E402
from flight_insights.params.files import read_param_file  # noqa: E402
from flight_insights.platforms.records import (  # noqa: E402
    create_platform,
    platform_view,
    save_answers,
)
from flight_insights.platforms.schema import load_schema  # noqa: E402
from flight_insights.recommend import (  # noqa: E402
    ExportError,
    Plan,
    export_card,
    export_revert,
    export_step,
    export_undo,
    flight_plan,
    history,
    plan_and_record,
)
from flight_insights.recommend.procedure import ProcedureStep, session  # noqa: E402
from flight_insights.store import StoreKeys, open_store  # noqa: E402
from flight_insights.store.cache import ColumnCache  # noqa: E402
from flight_insights.store.db import Store  # noqa: E402
from rehearsals import REHEARSALS, Rehearsal  # noqa: E402
from run_scenario import SERIAL0, ScenarioFailed, Vehicle, sitl_command  # noqa: E402

PERSON = ("task", "hardware", "firmware", "ground_check")  # steps for a person, not SITL


class NotASimulator(Exception):
    """The link is not to a simulator on this computer, so nothing is written to it."""


class RehearsalFailed(Exception):
    pass


# --- safety --------------------------------------------------------------------------------


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a name could resolve anywhere: only a literal loopback address


def check_simulator(vehicle: Vehicle, configured: tuple[str, int] = SERIAL0) -> None:
    """Refuse anything but ArduPilot SITL on this computer. The configured address, the
    address the link was opened to and the socket's peer must all be loopback, and the
    vehicle must answer for SIM_SPEEDUP, a parameter only the simulator has."""
    conn = vehicle.conn
    hosts = [configured[0]]
    if (dest := getattr(conn, "destination_addr", None)) is not None:
        hosts.append(str(dest[0]))
    port = getattr(conn, "port", None)
    with contextlib.suppress(AttributeError, OSError):
        hosts.append(str(port.getpeername()[0]))  # type: ignore[union-attr]
    if not all(_loopback(h) for h in hosts):
        raise NotASimulator(f"refusing a link to {', '.join(hosts)}: not this computer")
    try:
        vehicle.get_param("SIM_SPEEDUP")
    except ScenarioFailed:
        raise NotASimulator("refusing a vehicle with no SIM_ parameters: not a simulator") from None


class Simulator:
    """SITL started by this tool, in its own working folder. The only thing it writes to:
    every connection is checked before it is used."""

    def __init__(
        self,
        command: list[str],
        work: Path,
        restart: Callable[[float, int | None], list[str]] | None = None,
    ) -> None:
        self.work = work
        self.out = (work / "sitl.txt").open("ab")
        self.proc = subprocess.Popen(command, cwd=work, stdout=self.out, stderr=subprocess.STDOUT)
        self.links = 0
        self._vehicle: Vehicle | None = None
        self._restart = restart  # the command line for a payload and speed-up

    def power_cycle(self, payload_kg: float = 0.0, speedup: int | None = None) -> Vehicle:
        """A fresh pack: the simulator starts again in the same folder, keeping its
        EEPROM (every parameter), carrying `payload_kg` more than its frame model."""
        if self._restart is None:
            raise RehearsalFailed("this simulator cannot be started again")
        if self._vehicle is not None:
            self._vehicle.close()
            self._vehicle = None
        self.proc.terminate()
        self.proc.wait(timeout=30)
        self.proc = subprocess.Popen(
            self._restart(payload_kg, speedup), cwd=self.work, stdout=self.out,
            stderr=subprocess.STDOUT,
        )  # fmt: skip
        return self.connect()

    @property
    def vehicle(self) -> Vehicle:
        if self._vehicle is None:
            raise NotASimulator("no checked link to the simulator")
        return self._vehicle

    def connect(self) -> Vehicle:
        self._vehicle = None
        self.links += 1
        vehicle = Vehicle(self.work / f"link{self.links}.tlog")
        vehicle.wait_ready()
        check_simulator(vehicle)
        self._vehicle = vehicle
        return vehicle

    def set(self, name: str, value: float) -> None:
        self.vehicle.set_param(name, value)

    def params(self) -> dict[str, float]:
        return {name: value for name, value, _ in self.vehicle.fetch_params()}

    def reboot(self) -> None:
        """As a person reboots from the ground station: SITL starts itself again, in
        the same process and folder, keeping its EEPROM."""
        old = self.vehicle
        old.command(mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 1)
        old.conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
        old.close()
        self._vehicle = None
        time.sleep(1)
        if self.proc.poll() is not None:
            raise RehearsalFailed("the simulator exited on reboot")
        self.connect()

    def log_since(self, start: float) -> Path:
        """The newest onboard log written after `start` (wall time), once it stops growing."""
        logs = [p for p in (self.work / "logs").glob("*.BIN") if p.stat().st_mtime >= start]
        if not logs:
            raise RehearsalFailed("the simulator wrote no log for the flight")
        log = max(logs, key=lambda p: p.stat().st_mtime)
        size = -1
        while log.stat().st_size != size:
            size = log.stat().st_size
            self.vehicle.pump(1)
        return log

    def stop(self) -> None:
        if self._vehicle is not None:
            self._vehicle.close()
        self.proc.terminate()
        self.proc.wait(timeout=30)
        self.out.close()


# --- the app -------------------------------------------------------------------------------


class App:
    """The engine as the app runs it: a store, a platform with its setup answers, and
    one airframe whose flights are imported, analysed and planned."""

    def __init__(self, work: Path, answers: Mapping[str, Any], stack: contextlib.ExitStack) -> None:
        self.pack: Pack = load_pack()
        self.reg = Registry.load(self.pack)
        schema = load_schema(self.pack)
        keys = StoreKeys.generate()
        self.store: Store = stack.enter_context(open_store(work / "fleet.db", keys, "e" * 32))
        self.cache = ColumnCache(work / "cache", keys)
        self.platform = create_platform(self.store, schema, "SITL", "copter")
        version = platform_view(self.store, schema, self.platform)["reference_build"]["version"]
        save_answers(self.store, schema, self.platform, version, dict(answers))
        self.airframe: str | None = None

    def import_log(self, path: Path) -> str:
        batch = scan(self.store, self.pack, bytes(32), [path])
        decision = (
            Decision("f1", airframe_id=self.airframe)
            if self.airframe
            else Decision("f1", new_airframe=(self.platform, "SITL"))
        )
        proposals = propose(self.store, self.pack, batch)
        [res] = commit(self.store, self.pack, self.cache, batch, proposals, [decision])
        if res.status != "imported" or len(res.flights) != 1:
            raise RehearsalFailed(f"import gave {res.status} with {len(res.flights)} flights")
        flight = self.store.get("flight", res.flights[0])
        assert flight is not None
        self.airframe = flight["airframe_id"]
        return str(flight["id"])

    def plan(self, flight: str) -> tuple[Plan, dict[str, Any]]:
        """The plan as the app shows it, recorded for the airframe (grading earlier
        plans against this flight first), and the same plan as objects."""
        recorded = plan_and_record(self.store, self.cache, self.pack, flight)
        return flight_plan(self.store, self.cache, self.pack, flight), recorded

    def records(self) -> dict[str, dict[str, Any]]:
        assert self.airframe is not None
        return {r["id"]: r for r in history(self.store, self.airframe)}


# --- the report ----------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: Any = None


@dataclass
class Report:
    rehearsal: str
    checks: list[Check] = field(default_factory=list[Check])
    person: list[str] = field(default_factory=list[str])  # steps a person does, not SITL
    notes: list[str] = field(default_factory=list[str])
    error: str | None = None

    def check(self, name: str, passed: bool, detail: Any = None) -> bool:
        self.checks.append(Check(name, passed, detail))
        print(f"  {'ok  ' if passed else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        return passed

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  note {text}", flush=True)

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


def same(a: float | None, b: float | None) -> bool:
    """Equal as the autopilot stores them (float32)."""
    if a is None or b is None:
        return a is b
    f32 = struct.Struct("<f")
    return f32.unpack(f32.pack(a))[0] == f32.unpack(f32.pack(b))[0]


# --- one rehearsal -------------------------------------------------------------------------


def export(app: App, plan: Plan, out: Path) -> dict[str, Path]:
    """The plan's files as a person downloads them: "step N", "revert", "undo", "card"."""
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {
        f"step {n}": export_step(plan, app.reg, n, "mission planner")
        for n in sorted({r.step for r in plan.offered()})
    }
    for name, make in (("revert", export_revert), ("undo", export_undo)):
        with contextlib.suppress(ExportError):
            files[name] = make(plan, app.reg, "mission planner")
    files["card"] = export_card(plan, app.reg)
    paths: dict[str, Path] = {}
    for name, f in files.items():
        paths[name] = out / f["filename"]
        paths[name].write_text(f["text"], encoding="utf-8", newline="\n")
    return paths


def carry_out(
    sim: Simulator,
    steps: Sequence[ProcedureStep],
    files: Mapping[str, Path],
    listed: set[str],
    report: Report,
    prefix: str = "",
) -> dict[str, float]:
    """The session's parameter steps, in order, as a person carries them out from the
    ground station. Returns every value loaded."""
    loaded: dict[str, float] = {}
    seen = set(listed)  # what the ground station lists: refreshed on refresh and reboot
    for s in steps:
        if s.kind == "set":
            values = read_param_file(files[f"step {s.step}"]).values()
            hidden = sorted(n for n in values if n not in seen)
            report.check(
                f"{prefix}step {s.step}: every parameter is listed when loaded", not hidden, hidden
            )
            for name, value in values.items():
                if name in seen:
                    sim.set(name, value)
            loaded |= values
        elif s.kind in ("refresh", "reboot_refresh", "reboot"):
            if s.kind != "refresh":
                sim.reboot()
            now = sim.params()
            appeared = sorted(set(now) - seen)
            if appeared:
                report.note(f"after the {s.kind}: {len(appeared)} parameters appeared")
            seen = set(now)
            wrong = {k: (v, now.get(k)) for k, v in loaded.items() if not same(now.get(k), v)}
            report.check(f"{prefix}values hold after the {s.kind}", not wrong, wrong or None)
        elif s.kind == "temporary":
            missing = [p for p in s.params if p not in loaded]
            report.check(
                f"{prefix}the test-flight settings are in the files", not missing, missing or None
            )
    return loaded


def load_back(sim: Simulator, path: Path, expected: Mapping[str, float]) -> dict[str, Any]:
    """Load a revert or undo file; what reads back differently from `expected`."""
    values = read_param_file(path).values()
    for name, value in values.items():
        sim.set(name, value)
    now = sim.params()
    return {k: (v, now.get(k)) for k, v in expected.items() if not same(now.get(k), v)}


def rehearse(r: Rehearsal, ardupilot: Path, out: Path, work: Path) -> Report:
    report = Report(r.name)
    print(f"== {r.name}: {r.description}", flush=True)

    def restart(payload_kg: float, speedup: int | None) -> list[str]:
        scenario = replace(r.setup, payload_kg=payload_kg, speedup=speedup or r.setup.speedup)
        return sitl_command(scenario, ardupilot, work)

    sim = Simulator(sitl_command(r.setup, ardupilot, work), work, restart)
    try:
        with contextlib.ExitStack() as stack:
            app = App(work, r.answers, stack)
            _rehearse(r, sim, app, out / r.name, report)
    except Exception as e:  # any failure is the rehearsal's result, reported with the rest
        report.error = f"{type(e).__name__}: {e}"
        print(f"  FAIL {report.error}")
        traceback.print_exc()
    finally:
        sim.stop()
    return report


@dataclass
class Round:
    """One plan carried out: its files, its test-flight settings with their revert
    values, and what the aircraft had before its files were loaded."""

    files: dict[str, Path]
    temporary: dict[str, float]
    start: dict[str, float]


def _rehearse(r: Rehearsal, sim: Simulator, app: App, out: Path, report: Report) -> None:
    v = sim.connect()
    for name, value in r.setup.params.items():  # planted after boot, keeping the defaults
        sim.set(name, value(v.get_param(name)) if callable(value) else value)
    if r.work:
        _rehearse_work(r, sim, app, out, report)
        return

    # The weaker setup, flown.
    t0 = time.time()
    r.setup.fly(sim.vehicle)
    plan, recorded = app.plan(app.import_log(sim.log_since(t0)))
    raised = {s.code for rec in plan.recommendations for s in rec.sources}
    report.check("the weaker setup raises what it plants", set(r.expects) <= raised, r.expects)

    # Plan after plan, one test flight each, until what it plants has been on offer.
    pending = set(r.expects)
    last: Round | None = None
    for n in range(1, r.rounds + 1):
        on_offer = sorted({s.code for rec in plan.offered() for s in rec.sources})
        actions = sorted(rec.lead.code for rec in plan.recommendations if rec.param is None)
        report.note(f"plan {n} ({plan.stage}) offers {on_offer}; actions {actions}")
        if not on_offer:
            break
        pending -= set(on_offer)
        last = _carry_out_plan(app, sim, plan, out / f"plan{n}", report, n)
        t = time.time()
        r.test_flight(sim.vehicle)
        following, recorded_next = app.plan(app.import_log(sim.log_since(t)))
        _graded(app, recorded, report, n)
        if last.temporary:
            # The revert may wait for its stage, behind a problem the test flight found.
            follow = {
                rec.native
                for rec in following.recommendations
                if rec.lead.code == "followup.revert_test_settings" and rec.status != "blocked"
            }
            again = {rec.native for rec in following.offered() if rec.native_revert is not None}
            missing = sorted(set(last.temporary) - follow - again)
            held = {
                rec.native: (rec.lead.code, rec.status, rec.native_value)
                for rec in following.recommendations
                if rec.native in missing
            }
            report.check(
                f"plan {n + 1} takes plan {n}'s test-flight settings out, or needs them again",
                not missing,
                held or missing or None,
            )
            waiting = sorted(
                rec.lead.code for rec in following.recommendations if rec.param is None
            )
            report.note(f"plan {n + 1} ({following.stage}) holds actions {waiting}")
        plan, recorded = following, recorded_next
        if not pending:
            break
    report.check("everything the weaker setup plants was on offer", not pending, sorted(pending))

    # Last, the revert and undo files of the last plan carried out.
    if last is not None and last.temporary:
        back = load_back(sim, last.files["revert"], last.temporary)
        report.check("the revert file takes the test-flight settings out", not back, back or None)
    if last is not None and "undo" in last.files:
        names = read_param_file(last.files["undo"]).values()
        back = load_back(sim, last.files["undo"], {k: last.start[k] for k in names})
        report.check("the undo file puts back every value it changed", not back, back or None)


def _fly(app: App, sim: Simulator, fly: Callable[[Vehicle], None]) -> tuple[Plan, dict[str, Any]]:
    """One flight, imported and planned as the app does (grading earlier plans first)."""
    t = time.time()
    fly(sim.vehicle)
    return app.plan(app.import_log(sim.log_since(t)))


def _action(app: App, code: str) -> dict[str, Any]:
    [row] = [
        r for r in app.records().values() if r["finding_code"] == code and r["param"] is None
    ]  # fmt: skip
    return row


def _rehearse_work(r: Rehearsal, sim: Simulator, app: App, out: Path, report: Report) -> None:
    """An action whose work is flights of its own, as the gate flies it: the test flight
    light and loaded, the plan's files loaded, the work flown on fresh packs (AutoTune,
    one axis a pack), then the test flight light and loaded again, which grades it."""
    [code] = r.expects
    for fly in r.before:  # the airframe's earlier flights
        _fly(app, sim, fly)

    # The weaker setup, flown light and loaded: two payload bands, before the change.
    plan, _ = _fly(app, sim, r.setup.fly)
    sim.power_cycle(r.loaded_kg)
    plan, recorded = _fly(app, sim, r.setup.fly)
    mine = [rec for rec in plan.recommendations if rec.lead.code == code]
    actions = [rec for rec in mine if rec.param is None]
    report.check(
        "the weaker setup offers the action, with what prepares it",
        [rec.status for rec in actions] == ["next"],
        {"on offer": [(rec.param, rec.proposed, rec.status) for rec in mine], "stage": plan.stage},
    )
    if [rec.status for rec in actions] != ["next"]:
        return
    done = _carry_out_plan(app, sim, plan, out / "plan1", report, 1)
    prepared = {
        rec["id"]: rec["param"] for rec in recorded["recommendations"]
        if rec["source"] == f"finding:{code}" and rec["canonical"] is not None
    }  # fmt: skip

    # The work, each on a fresh pack, with its own settings for that flight only.
    for n, w in enumerate(r.work, 1):
        v = sim.power_cycle(0.0, w.speedup)
        before = {name: v.get_param(name) for name in w.params}
        for name, value in w.params.items():
            sim.set(name, value)
        _fly(app, sim, w.fly)
        for name, value in before.items():
            sim.set(name, value)
        action = _action(app, code)
        report.note(f"{w.name}: {action['status']}, axes {action['detail'].get('applied_axes')}")
        if n < len(r.work):
            report.check(f"{w.name}: not done until every axis is", action["status"] != "applied")
    action = _action(app, code)
    report.check(
        "the work's own flights show it done",
        action["status"] == "applied" and bool(action["detail"].get("applied_flight")),
        action["detail"].get("applied_axes"),
    )
    records = app.records()
    report.check(
        "what prepares it waits for it: not graded on its flights",
        all("grade" not in records[i]["detail"] and "grade_pending" not in records[i]["detail"]
            for i in prepared),
    )  # fmt: skip

    # The test flight again, light then loaded: pending, then graded.
    sim.power_cycle(0.0)
    _fly(app, sim, r.test_flight)
    action = _action(app, code)
    pending = action["detail"].get("grade_pending", {})
    report.check(
        "light alone leaves the grade waiting for the loaded band",
        action["status"] == "applied" and bool(pending),
        _bands(pending.get("checks", ())),
    )
    sim.power_cycle(r.loaded_kg)
    _fly(app, sim, r.test_flight)
    records = app.records()
    for rid in [action["id"], *prepared]:
        row = records[rid]
        grade = row["detail"].get("grade") or row["detail"].get("grade_pending") or {}
        checks = grade.get("checks", ())
        report.check(
            f"{row['param'] or code} confirmed, light and loaded",
            row["status"] == "confirmed"
            and not any(c["passed"] is False for c in checks)
            and all(c["passed"] is True for c in checks if c.get("required")),
            {"status": row["status"], "checks": _bands(checks)},
        )

    # Last, the undo file of the plan carried out puts back what the flight had,
    # the gains the work saved included.
    if "undo" in done.files:
        names = read_param_file(done.files["undo"]).values()
        back = load_back(sim, done.files["undo"], {k: done.start[k] for k in names})
        report.check("the undo file puts back the gains the work saved", not back, back or None)


def _bands(checks: Sequence[Mapping[str, Any]]) -> list[Any]:
    """Each measured check's result by payload band, or light against heavy, for the report."""
    out: list[Any] = []
    for c in checks:
        if c.get("check") == "metric_improves":
            bands = [(b["group"], b["verdict"], b.get("change")) for b in c.get("bands", ())]
            out.append((c["metric"], c["passed"], bands))
        elif c.get("check") == "no_shift_worse":
            out.append(("no_shift_worse", c["passed"], c.get("shifts")))
        else:
            out.append((c.get("metric"), c["passed"], c.get("light"), c.get("heavy")))
    return out


def _carry_out_plan(
    app: App, sim: Simulator, plan: Plan, out: Path, report: Report, n: int
) -> Round:
    """A plan's files, exported and loaded as its procedure says."""
    start = sim.params()
    files = export(app, plan, out)
    steps = session(plan, app.reg)
    (out / "procedure.json").write_text(
        json.dumps([{"kind": s.kind, "text": s.text["en"]} for s in steps], indent=1)
    )
    report.person += [s.text["en"] for s in steps if s.kind in PERSON]
    if "undo" in files:
        undo = read_param_file(files["undo"]).values()
        wrong = {k: (v, start.get(k)) for k, v in undo.items() if not same(start.get(k), v)}
        report.check(
            f"plan {n}: its undo file holds what the aircraft had", not wrong, wrong or None
        )
    loaded = carry_out(sim, steps, files, set(start), report, f"plan {n}: ")
    now = sim.params()
    wrong = {k: (v, now.get(k)) for k, v in loaded.items() if not same(now.get(k), v)}
    report.check(f"plan {n}: every value in its files is on the aircraft", not wrong, wrong or None)
    temporary = {
        rec.native: float(rec.native_revert)
        for rec in plan.offered()
        if rec.native is not None and rec.native_revert is not None
    }
    return Round(files, temporary, start)


def _graded(app: App, recorded: Mapping[str, Any], report: Report, n: int) -> None:
    """Every change a plan offered is confirmed by its test flight, each check passed."""
    records = app.records()
    for rec in recorded["recommendations"]:
        if rec["status"] != "next" or rec["canonical"] is None:
            continue
        row = records[rec["id"]]
        grade = row["detail"].get("grade") or {}
        checks = [(c["check"], c["passed"]) for c in grade.get("checks", ())]
        report.check(
            f"plan {n}: {rec['param']} -> {rec['proposed']} confirmed by the test flight",
            row["status"] == "confirmed" and all(p is True for _, p in checks),
            {"status": row["status"], "after": grade.get("after"), "checks": checks},
        )


# --- the command ---------------------------------------------------------------------------


@contextlib.contextmanager
def _folder(path: Path | None) -> Iterator[Path]:
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        yield path
        return
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("rehearsals", nargs="*", help=f"any of {', '.join(sorted(REHEARSALS))}")
    parser.add_argument("--ardupilot", required=True, type=Path, help="ArduPilot checkout, built")
    parser.add_argument("--version", required=True, help="firmware version, e.g. 4.7.0")
    parser.add_argument("--out", required=True, type=Path, help="for the files and the report")
    parser.add_argument("--work", type=Path, help="keep the simulator's folders here")
    args = parser.parse_args(argv)
    unknown = sorted(set(args.rehearsals) - set(REHEARSALS))
    if unknown:
        parser.error(f"no rehearsal {', '.join(unknown)}")
    reports: list[Report] = []
    for name in args.rehearsals or sorted(REHEARSALS):
        with _folder(args.work / name if args.work else None) as work:
            reports.append(rehearse(REHEARSALS[name], args.ardupilot, args.out, work))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(
        json.dumps(
            {
                "version": args.version,
                "rehearsals": [asdict(r) | {"passed": r.passed} for r in reports],
            },
            indent=1,
        )
    )
    failed = [r.rehearsal for r in reports if not r.passed]
    print(f"{len(reports) - len(failed)} of {len(reports)} rehearsals passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
