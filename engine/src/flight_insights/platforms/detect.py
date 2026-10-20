"""Pre-filling setup answers from a parameter file or a bench log.

Every detected value says where it came from, so the person confirming it can
check it. A bench log gives more than a parameter file: the board, firmware,
battery voltage and the sensors that actually reported.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from flight_insights.dataflash import read_dataflash
from flight_insights.dataflash.formats import text
from flight_insights.dataflash.log import DataflashLog
from flight_insights.fingerprint import fingerprint
from flight_insights.history.snapshot import Snapshot, from_param_file
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect
from flight_insights.params.files import read_param_file
from flight_insights.platforms.schema import SetupError

# Motors per frame class, by the names in ArduPilot's FRAME_CLASS and Q_FRAME_CLASS
# metadata (AP_Motors: matrix frames, plus the single, coaxial and heli types).
MOTORS = {
    "Quad": 4, "Hexa": 6, "Octa": 8, "OctaQuad": 8, "Y6": 6, "Tri": 3, "Deca": 10,
    "DodecaHexa": 12, "BiCopter": 2, "SingleCopter": 1, "CoaxCopter": 2,
    "Heli": 1, "Heli_Dual": 2, "HeliQuad": 4,
}  # fmt: skip
VOLTS_PER_CELL_MAX = 4.2  # MOT_BAT_VOLT_MAX for a LiPo pack, per cell


def _entry(value: Any, source: str) -> dict[str, Any]:
    return {"value": value, "source": f"detected: {source}"}


class _Params:
    def __init__(self, snapshot: Snapshot, pack: Pack) -> None:
        self.snapshot = snapshot
        self.dialect: Dialect = snapshot.dialect(pack)
        self.values = self.dialect.to_canonical(snapshot.values).values

    def get(self, name: str) -> float | None:
        return self.values.get(name)

    def set_away_from_default(self, name: str) -> float | None:
        native, _ = self.dialect.native(name)
        return None if self.snapshot.is_default(native) else self.get(name)

    def label(self, name: str) -> str | None:
        v = self.get(name)
        if v is None:
            return None
        native, _ = self.dialect.native(name)
        return self.dialect.meta.get(native, {}).get("values", {}).get(str(round(v)), str(round(v)))

    def on(self, name: str) -> bool:
        v = self.get(name)
        return v is not None and v != 0


def _from_params(p: _Params, vehicle: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {"vehicle": _entry(vehicle, "parameters")}
    quad = vehicle == "plane" and p.on("Q_ENABLE")
    frame = "Q_FRAME_CLASS" if quad else "FRAME_CLASS"
    if (cls := p.label(frame)) is not None:
        out["frame_class"] = _entry(cls, f"{frame} {round(p.get(frame) or 0)}")
        if cls in MOTORS:
            out["motor_count"] = _entry(MOTORS[cls], f"{frame} {cls}")
    ftype = "Q_FRAME_TYPE" if quad else "FRAME_TYPE"
    heli = (out.get("frame_class") or {}).get("value") in ("Heli", "Heli_Dual")
    if (t := p.label(ftype)) is not None and not heli:  # a helicopter has no motor layout
        out["frame_type"] = _entry(t, f"{ftype} {round(p.get(ftype) or 0)}")
    pwm = "Q_M_PWM_TYPE" if quad else "MOT_PWM_TYPE"
    if (proto := p.label(pwm)) is not None:
        out["esc_protocol"] = _entry(proto, pwm)
        dshot = "DShot" in proto
        bdshot = dshot and p.on("SERVO_BLH_BDMASK")
        out["esc_rpm_telemetry"] = _entry(bdshot, f"{pwm}, SERVO_BLH_BDMASK")
    if (cap := p.set_away_from_default("BATT_CAPACITY")) is not None:
        out["capacity_mah"] = _entry(cap, "BATT_CAPACITY")
    if (vmax := p.set_away_from_default("MOT_BAT_VOLT_MAX")) is not None and vmax > 0:
        out["cells"] = _entry(round(vmax / VOLTS_PER_CELL_MAX), f"MOT_BAT_VOLT_MAX {vmax:g} V")
    gps = [n for n in ("GPS1_TYPE", "GPS2_TYPE") if p.on(n)]
    out["gnss_count"] = _entry(len(gps), ", ".join(gps) or "GPS1_TYPE")
    compasses = [n for n in ("COMPASS_USE", "COMPASS_USE2", "COMPASS_USE3") if p.on(n)]
    if compasses:
        out["compass_count"] = _entry(len(compasses), ", ".join(compasses))
    for field, name in (
        ("rangefinder", "RNGFND1_TYPE"),
        ("optical_flow", "FLOW_TYPE"),
        ("visual_odometry", "VISO_TYPE"),
    ):
        if (label := p.label(name)) is not None:
            out[field] = _entry(label if p.on(name) else "none", name)
    return out


def _vehicle_of_params(names: set[str]) -> str:
    if "FRAME_CLASS" in names:
        return "copter"
    if names & {"Q_ENABLE", "TECS_CLMB_MAX", "AIRSPEED_CRUISE"}:
        return "plane"
    raise SetupError("cannot tell from the file whether this is a copter or a plane")


def detect(path: Path, pack: Pack) -> dict[str, dict[str, Any]]:
    """Setup answers read from a .param/.params file or a .bin log."""
    if path.suffix.lower() == ".bin":
        return _from_log(path, pack)
    params = read_param_file(path)
    names = {e.name for e in params.entries}
    vehicle = _vehicle_of_params(names)
    # A parameter file does not name its firmware: read it in the newest dialect.
    snapshot = from_param_file(params, vehicle, pack.versions[-1])
    return _from_params(_Params(snapshot, pack), vehicle)


def _from_log(path: Path, pack: Pack) -> dict[str, dict[str, Any]]:
    from flight_insights.history.snapshot import from_log

    with read_dataflash(path) as df:
        snapshot = from_log(df)
        fp = fingerprint(df, pack, secrets.token_bytes(32))
        rc = _rc_protocol(df)
    out = _from_params(_Params(snapshot, pack), snapshot.vehicle)
    out["vehicle"] = _entry(snapshot.vehicle, "VER")
    out["firmware"] = _entry(snapshot.firmware, "VER")
    if fp.board.name:
        detail = f"{fp.board.name}, {fp.board.mcu}" if fp.board.mcu else fp.board.name
        out["board"] = _entry(fp.board.name, f"system-ID message ({detail})")
    if fp.cells.value is not None:
        out["cells"] = _entry(fp.cells.value, f"BAT.Volt ({fp.cells.note})")
    gps = fp.gps.value or {}
    if gps.get("reporting"):
        out["gnss_count"] = _entry(gps["reporting"], "GPS instances reporting")
        out["rtk"] = _entry(bool(gps.get("rtk")), "GPS status")
    if fp.compasses.value:
        out["compass_count"] = _entry(fp.compasses.value, "MAG instances reporting")
    if "ESC telemetry" in (fp.rpm_sources.value or []):
        out["esc_rpm_telemetry"] = _entry(True, "ESC.RPM reported")
    if rc:
        out["rc_protocol"] = _entry(rc, "MSG 'RC Protocol'")
    return out


def _rc_protocol(df: DataflashLog) -> str | None:
    if "MSG" not in df.message_types:
        return None
    for row in df.table("MSG").to_pylist():
        message = text(row["Message"])
        if message.startswith("RC Protocol: "):
            return message.removeprefix("RC Protocol: ").strip()
    return None
