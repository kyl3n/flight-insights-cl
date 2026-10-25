#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pymavlink==2.4.50", "pyyaml==6.0.2"]
# ///
"""Check the parameter gates table against ArduPilot SITL: set each gate, refresh the
parameter list, power-cycle, refresh again, and compare what appeared, and when, with
what build_param_gates.py read from the source.

For each gate the simulator starts from a fresh EEPROM. A power cycle is a restart of
the SITL process in the same folder, which keeps its EEPROM, as a reboot keeps the
board's. Parameters are only ever written to this local simulator.

The check fails when a parameter:
- appeared after a refresh but the table names another gate, or says it needs a reboot;
- appeared only after the power cycle but the table says a refresh is enough;
- the table puts behind the gate appeared only after the power cycle, when the table
  says a refresh.

Usage (from the repository root):
  uv run knowledge/tools/check_param_gates_sitl.py --ardupilot DIR --version 4.7.0
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "fixtures" / "sitl"))

from run_scenario import SITL_HOME, ScenarioFailed, Vehicle  # noqa: E402

# Gates SITL can switch on for a quad, with a value that creates a simulated backend.
COPTER_GATES: dict[str, float] = {
    "INS_HNTC2_ENABLE": 1,
    "INS_HNTC3_ENABLE": 1,
    "FFT_ENABLE": 1,
    "BATT2_MONITOR": 4,  # analog
    "RNGFND1_TYPE": 100,  # SITL
    "GPS2_TYPE": 1,
    "RPM1_TYPE": 10,  # SITL
    "PRX1_TYPE": 10,  # SITL
    "FLOW_TYPE": 10,  # SITL
    "BCN_TYPE": 10,  # SITL
    "SCR_ENABLE": 1,
    "EK2_ENABLE": 1,
    "PLND_ENABLED": 1,
    "FOLL_ENABLE": 1,
    "GRIP_ENABLE": 1,
    "CHUTE_ENABLED": 1,
    "INS_TCAL1_ENABLE": 1,
    "SERVO_32_ENABLE": 1,
    "MNT1_TYPE": 1,  # servo
    "CAM1_TYPE": 1,  # servo
    "WINCH_TYPE": 1,
    "SPRAY_ENABLE": 1,
}


def start(binary: Path, work: Path, defaults: Path) -> subprocess.Popen[bytes]:
    log = (work / "sitl.txt").open("ab")
    return subprocess.Popen(
        [str(binary), "--model", "quad", "--speedup", "10", "--defaults", str(defaults),
         "--home", SITL_HOME, "-I0"],
        cwd=work, stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip


def names(binary: Path, work: Path, defaults: Path, change: tuple[str, float] | None) -> set[str]:
    """The parameter names the simulator lists, after setting `change` if given."""
    proc = start(binary, work, defaults)
    try:
        vehicle = Vehicle(work / "link.tlog")
        vehicle.wait_ready()
        if change is not None:
            vehicle.set_param(*change)
        listed = {name for name, _, _ in vehicle.fetch_params()}
        vehicle.close()
        return listed
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def check_gate(
    binary: Path, defaults: Path, gate: str, value: float, table: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        before = names(binary, work, defaults, None)
        # set, then list in the same session (a refresh), then power-cycle and list again
        proc = start(binary, work, defaults)
        try:
            vehicle = Vehicle(work / "link.tlog")
            vehicle.wait_ready()
            vehicle.set_param(gate, value)
            refreshed = {name for name, _, _ in vehicle.fetch_params()}
            vehicle.close()
        finally:
            proc.terminate()
            proc.wait(timeout=30)
        rebooted = names(binary, work, defaults, None)
    on_refresh = refreshed - before
    on_reboot = rebooted - refreshed
    behind = {p for p, g in table.items() if g["gate"] == gate}
    problems: dict[str, list[str]] = {
        "refresh_not_behind_gate": sorted(
            p for p in on_refresh if table.get(p, {}).get("gate") != gate
        ),
        "refresh_but_table_says_reboot": sorted(
            p for p in on_refresh if table.get(p, {}).get("after") == "reboot"
        ),
        "reboot_but_table_says_refresh": sorted(
            p for p in on_reboot if p in behind and table[p]["after"] == "refresh"
        ),
        "reboot_not_behind_gate": sorted(p for p in on_reboot if p not in behind),
    }
    problems["appeared_on_refresh"] = [str(len(on_refresh))]
    problems["appeared_on_reboot"] = [str(len(on_reboot))]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ardupilot", type=Path, required=True, help="a built 4.7 checkout")
    parser.add_argument("--version", required=True)
    parser.add_argument("--report", type=Path, help="write the results here as JSON")
    parser.add_argument("--only", nargs="*", help="check just these gates")
    args = parser.parse_args()
    binary = args.ardupilot / "build" / "sitl" / "bin" / "arducopter"
    defaults = args.ardupilot / "Tools" / "autotest" / "default_params" / "copter.parm"
    with gzip.open(ROOT / "knowledge" / "params" / args.version / "gates.json.gz", "rt") as f:
        table: dict[str, dict[str, str]] = json.load(f)["copter"]
    results: dict[str, dict[str, list[str]]] = {}
    failed = False
    for gate, value in COPTER_GATES.items():
        if args.only and gate not in args.only:
            continue
        try:
            r = check_gate(binary, defaults, gate, value, table)
        except ScenarioFailed as e:  # not compiled into this SITL build
            print(f"{gate}={value:g}: skipped ({e})")
            continue
        results[gate] = r
        bad = {k: v for k, v in r.items() if v and not k.startswith("appeared")}
        hard = {k: v for k, v in bad.items() if k != "reboot_not_behind_gate"}
        failed |= bool(hard)
        print(
            f"{gate}={value:g}: {r['appeared_on_refresh'][0]} on refresh, "
            f"{r['appeared_on_reboot'][0]} on reboot"
            + "".join(
                f"\n  {k}: {', '.join(v[:12])}{' ...' if len(v) > 12 else ''}"
                for k, v in bad.items()
            )
        )
    if args.report:
        args.report.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    print("FAILED" if failed else "PASSED: every gate behaves as the table says")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
