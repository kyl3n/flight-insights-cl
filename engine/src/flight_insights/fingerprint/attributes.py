"""The vehicle fingerprint: what is flying, read from the log's parameters and messages
(spec: Vehicle fingerprinting).

Every attribute carries where it came from, so a person confirming or correcting
the fingerprint can see why it says what it says. Names of parameter values
(frame classes, output protocols, notch modes...) come from the release's own
parameter metadata.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from flight_insights.dataflash import si
from flight_insights.dataflash.log import DataflashLog
from flight_insights.fingerprint.board import Board, read_board
from flight_insights.history.snapshot import Snapshot, from_log
from flight_insights.knowledge import Pack
from flight_insights.params.dialect import Dialect
from flight_insights.segment import (
    Flight,
    HoverRules,
    HoverWindow,
    find_boots,
    flights,
    hover_windows,
    timeline,
)

HELI_FRAME_CLASSES = {6: "single", 11: "dual", 13: "quad"}  # FRAME_CLASS: Heli, Heli_Dual, HeliQuad
DSHOT_FIRST, DSHOT_LAST = 4, 7  # MOT_PWM_TYPE DShot150 ... DShot1200
CELL_MIN_V, CELL_MAX_V, CELL_TYPICAL_V = 3.0, 4.4, 3.9
CELL_FULL_V = (4.1, 4.4)  # a full cell: LiPo and Li-ion 4.2 V, HV LiPo 4.35 V
RTK_FLOAT = 5  # GPS.Status: 5 RTK float, 6 RTK fixed


@dataclass(frozen=True)
class Attr[T]:
    value: T | None
    source: str  # the parameters or messages it was read from
    note: str = ""


@dataclass(frozen=True)
class Frame:
    vehicle_class: str  # multirotor, helicopter, plane, quadplane
    frame_class: str | None  # metadata name, such as Quad
    frame_type: str | None  # X, Plus...
    heli: str | None = None  # single, dual, quad
    vtol: str | None = None  # standard, tiltrotor, tailsitter (QuadPlane only)


@dataclass(frozen=True)
class Fingerprint:
    vehicle: str
    firmware: str
    board: Board
    imu_count: Attr[int]
    imu_rates_hz: Attr[dict[int, tuple[float, float]]]  # instance -> (gyro, accel)
    frame: Attr[Frame]
    motor_protocol: Attr[str]
    bidirectional_dshot: Attr[bool]
    rpm_sources: Attr[list[str]]
    notch_mode: Attr[str]
    cells: Attr[int]
    capacity_mah: Attr[float]
    hover_throttle: Attr[float]  # measured in steady hover
    learned_hover_throttle: Attr[float]
    motor_hz_at_hover: Attr[float]  # propulsion fundamental from ESC RPM
    gps: Attr[dict[str, Any]]
    compasses: Attr[int]
    other_nav: Attr[list[str]]
    ekf_sources: Attr[dict[str, str]]
    hover_s: float
    flight_s: float
    mode_s: dict[str, float] = field(default_factory=dict[str, float])
    hover_windows: list[HoverWindow] = field(default_factory=list[HoverWindow])
    rules_calibrated: bool = False
    params: dict[str, float] = field(default_factory=dict[str, float])  # canonical names
    # Parameters set away from the firmware default: chosen for this vehicle.
    configured: dict[str, float] = field(default_factory=dict[str, float])


class _Params:
    """Parameter lookups by canonical name, with value names from the metadata."""

    def __init__(self, snapshot: Snapshot, pack: Pack) -> None:
        self.dialect: Dialect = snapshot.dialect(pack)
        self.values = self.dialect.to_canonical(snapshot.values).values
        self.configured = self.dialect.to_canonical(
            {n: v for n, v in snapshot.values.items() if not snapshot.is_default(n)}
        ).values

    def get(self, name: str) -> float | None:
        return self.values.get(name)

    def label(self, name: str) -> str | None:
        v = self.get(name)
        if v is None:
            return None
        native, _ = self.dialect.native(name)
        names: Mapping[str, str] = self.dialect.meta.get(native, {}).get("values", {})
        return names.get(str(round(v)), str(round(v)))

    def on(self, name: str) -> bool:
        v = self.get(name)
        return v is not None and v != 0


def _instances(df: DataflashLog, name: str, column: str) -> list[int]:
    seen: set[int] = set()
    for fid in df.formats_named(name):
        if df.count_format(fid) and column in df.index.formats[fid].field_names:
            seen |= {int(x) for x in np.unique(df.field(fid, column))}
    return sorted(seen)


def _in_windows(t: np.ndarray[Any, Any], windows: list[HoverWindow]) -> np.ndarray[Any, Any]:
    mask = np.zeros(len(t), dtype=bool)
    for w in windows:
        mask |= (t >= w.start_us) & (t <= w.end_us)
    return mask


def _frame(p: _Params, vehicle: str) -> Attr[Frame]:
    if vehicle == "plane":
        if p.on("Q_ENABLE"):
            vtol = (
                "tiltrotor" if p.on("Q_TILT_ENABLE")
                else "tailsitter" if p.on("Q_TAILSIT_ENABLE") else "standard"
            )  # fmt: skip
            frame = Frame("quadplane", p.label("Q_FRAME_CLASS"), p.label("Q_FRAME_TYPE"), vtol=vtol)
            return Attr(
                frame, "Q_ENABLE, Q_FRAME_CLASS, Q_FRAME_TYPE, Q_TILT_ENABLE, Q_TAILSIT_ENABLE"
            )
        return Attr(Frame("plane", None, None), "vehicle type")
    cls = p.get("FRAME_CLASS")
    heli = HELI_FRAME_CLASSES.get(round(cls)) if cls is not None else None
    frame = Frame(
        "helicopter" if heli else "multirotor",
        p.label("FRAME_CLASS"),
        None if heli else p.label("FRAME_TYPE"),
        heli=heli,
    )
    return Attr(frame, "FRAME_CLASS, FRAME_TYPE")


def _cells(
    df: DataflashLog, volt_max: float | None = None, volt_max_name: str = "MOT_BAT_VOLT_MAX"
) -> Attr[int]:
    """Cell count from the highest resting voltage of the first battery. Several counts
    can fit one voltage (50 V is a full 12S or a half-empty 13S), so the voltage scaling
    the aircraft is set up with, `MOT_BAT_VOLT_MAX` (a full cell's 4.2 V, or 4.35 V for
    HV packs, times the count), settles it when it agrees."""
    if "BAT" not in df.message_types:
        return Attr(None, "BAT", "no battery messages")
    bat = df.table("BAT")
    inst = "Inst" if "Inst" in bat.column_names else "I"
    volts = si.values(bat, "Volt")
    if inst in bat.column_names:
        volts = volts[np.asarray(bat.column(inst).to_numpy()) == 0]
    volts = volts[np.isfinite(volts) & (volts > 0)]
    if not len(volts):
        return Attr(None, "BAT.Volt", "no voltage reading")
    v = float(np.percentile(volts, 99))
    candidates = [n for n in range(1, 25) if CELL_MIN_V <= v / n <= CELL_MAX_V]
    if not candidates:
        return Attr(None, "BAT.Volt", f"{v:.2f} V fits no whole number of cells")
    if volt_max:
        set_up = [n for n in candidates if CELL_FULL_V[0] <= volt_max / n <= CELL_FULL_V[1]]
        if set_up:
            n = min(set_up, key=lambda c: abs(volt_max / c - 4.2))
            note = f"{v:.2f} V, {v / n:.2f} V per cell; voltage scaling set for {volt_max:g} V"
            return Attr(n, f"BAT.Volt (instance 0), {volt_max_name}", note)
    n = min(candidates, key=lambda c: abs(v / c - CELL_TYPICAL_V))
    note = f"{v:.2f} V, {v / n:.2f} V per cell"
    if len(candidates) > 1:
        note += f"; also possible: {', '.join(str(c) for c in candidates if c != n)}"
    if volt_max:
        note += f"; {volt_max_name} {volt_max:g} V fits none of these"
    return Attr(n, "BAT.Volt (instance 0)", note)


def _hover_throttle(df: DataflashLog, vehicle: str, windows: list[HoverWindow]) -> Attr[float]:
    msg = "QTUN" if vehicle == "plane" else "CTUN"
    if not windows or msg not in df.message_types:
        return Attr(None, f"{msg}.ThO", "no steady hover")
    t = df.table(msg)
    if "ThO" not in t.column_names:
        return Attr(None, f"{msg}.ThO", "no ThO column")
    times = np.asarray(t.column("TimeUS").to_numpy(), dtype=np.int64)
    tho = si.values(t, "ThO")[_in_windows(times, windows)]
    if not len(tho):
        return Attr(None, f"{msg}.ThO", "no samples in hover")
    return Attr(float(np.median(tho)), f"{msg}.ThO, median in steady hover", f"{len(tho)} samples")


def _motor_hz(df: DataflashLog, windows: list[HoverWindow]) -> Attr[float]:
    if "ESC" not in df.message_types:
        return Attr(None, "ESC.RPM", "no ESC telemetry")
    if not windows:
        return Attr(None, "ESC.RPM", "no steady hover")
    esc = df.table("ESC")
    times = np.asarray(esc.column("TimeUS").to_numpy(), dtype=np.int64)
    rpm = si.values(esc, "RPM")[_in_windows(times, windows)]
    rpm = rpm[rpm > 0]
    if not len(rpm):
        return Attr(None, "ESC.RPM", "no RPM in hover")
    return Attr(float(np.median(rpm)) / 60.0, "ESC.RPM, median of all motors in steady hover",
                f"{len(rpm)} samples")  # fmt: skip


def fingerprint(df: DataflashLog, pack: Pack, board_key: bytes) -> Fingerprint:
    snapshot = from_log(df)
    vehicle, firmware = snapshot.vehicle, snapshot.firmware
    p = _Params(snapshot, pack)
    boots = find_boots(timeline(df))
    flown: list[Flight] = flights(df, boots, pack, vehicle, firmware)
    rules = HoverRules.load(pack)
    windows = hover_windows(df, boots, flown, vehicle, rules)

    imus = _instances(df, "IMU", "I")
    rates: dict[int, tuple[float, float]] = {}
    if "IMU" in df.message_types and {"GHz", "AHz"} <= set(df.table("IMU").column_names):
        imu = df.table("IMU")
        inst = np.asarray(imu.column("I").to_numpy())
        for i in imus:
            sel = inst == i
            rates[i] = (
                float(np.median(si.values(imu, "GHz")[sel])),
                float(np.median(si.values(imu, "AHz")[sel])),
            )

    q = vehicle == "plane" and p.on("Q_ENABLE")
    pwm_name = "Q_M_PWM_TYPE" if q else "MOT_PWM_TYPE"
    volt_max_name = "Q_M_BAT_VOLT_MAX" if q else "MOT_BAT_VOLT_MAX"
    pwm = p.get(pwm_name)
    dshot = pwm is not None and DSHOT_FIRST <= round(pwm) <= DSHOT_LAST
    bdshot = dshot and p.on("SERVO_BLH_BDMASK")

    sources: list[str] = []
    if "ESC" in df.message_types and float(np.nanmax(si.values(df.table("ESC"), "RPM"))) > 0:
        sources.append("ESC telemetry")
    if "RPM" in df.message_types or p.on("RPM1_TYPE"):
        sources.append("RPM sensor")
    if p.on("FFT_ENABLE"):
        sources.append("in-flight FFT")
    notch = p.label("INS_HNTCH_MODE") if p.on("INS_HNTCH_ENABLE") else "off"

    gps_types = [n for n in ("GPS1_TYPE", "GPS2_TYPE") if p.on(n)]
    gps_status = 0
    for fid in df.formats_named("GPS"):
        if df.count_format(fid) and "Status" in df.index.formats[fid].field_names:
            gps_status = max(gps_status, int(df.field(fid, "Status").max()))
    gps = {
        "configured": len(gps_types),
        "reporting": len(_instances(df, "GPS", "I")),
        "rtk": gps_status >= RTK_FLOAT,
        "best_status": gps_status,
    }
    other_nav = [
        label
        for name, label in (
            ("RNGFND1_TYPE", "rangefinder"),
            ("FLOW_TYPE", "optical flow"),
            ("VISO_TYPE", "external navigation"),
            ("BCN_TYPE", "beacons"),
        )
        if p.on(name)
    ]
    ekf = {
        n: p.label(f"EK3_SRC1_{n}") or ""
        for n in ("POSXY", "VELXY", "POSZ", "VELZ", "YAW")
        if p.get(f"EK3_SRC1_{n}") is not None
    }
    mode_s: dict[str, float] = {}
    for f in flown:
        for m in f.modes:
            span = (min(m.end_us, f.disarm_us) - max(m.start_us, f.arm_us)) / 1e6
            mode_s[m.name] = mode_s.get(m.name, 0.0) + max(span, 0.0)

    learned = "Q_M_THST_HOVER" if q else "MOT_THST_HOVER"
    return Fingerprint(
        vehicle=vehicle,
        firmware=firmware,
        board=read_board(df, pack, firmware, board_key),
        imu_count=Attr(len(imus), "IMU instances"),
        imu_rates_hz=Attr(rates, "IMU.GHz, IMU.AHz (median)"),
        frame=_frame(p, vehicle),
        motor_protocol=Attr(p.label(pwm_name), pwm_name),
        bidirectional_dshot=Attr(bdshot, f"{pwm_name}, SERVO_BLH_BDMASK"),
        rpm_sources=Attr(sources, "ESC.RPM, RPM messages, RPM1_TYPE, FFT_ENABLE"),
        notch_mode=Attr(notch, "INS_HNTCH_ENABLE, INS_HNTCH_MODE"),
        cells=_cells(df, p.get(volt_max_name), volt_max_name),
        capacity_mah=Attr(p.get("BATT_CAPACITY"), "BATT_CAPACITY"),
        hover_throttle=_hover_throttle(df, vehicle, windows),
        learned_hover_throttle=Attr(p.get(learned), learned),
        motor_hz_at_hover=_motor_hz(df, windows),
        gps=Attr(gps, "GPS1_TYPE, GPS2_TYPE, GPS instances and status"),
        compasses=Attr(len(_instances(df, "MAG", "I")), "MAG instances"),
        other_nav=Attr(other_nav, "RNGFND1_TYPE, FLOW_TYPE, VISO_TYPE, BCN_TYPE"),
        ekf_sources=Attr(ekf, "EK3_SRC1_*"),
        hover_s=sum(w.duration_s for w in windows),
        flight_s=sum(f.duration_s for f in flown),
        mode_s=mode_s,
        hover_windows=windows,
        rules_calibrated=rules.calibrated,
        params=p.values,
        configured=p.configured,
    )


def parameter_record(snapshot: Snapshot, pack: Pack) -> dict[str, Any]:
    """What a parameter file alone says about the aircraft, shaped like a flight's
    fingerprint record: the attributes read from parameters, each with its source.
    Everything only a log shows (board, IMUs, hover, ESC telemetry) is left out, so
    analyzers read it as unknown."""
    p = _Params(snapshot, pack)
    vehicle = snapshot.vehicle
    q = vehicle == "plane" and p.on("Q_ENABLE")
    pwm_name = "Q_M_PWM_TYPE" if q else "MOT_PWM_TYPE"
    pwm = p.get(pwm_name)
    dshot = pwm is not None and DSHOT_FIRST <= round(pwm) <= DSHOT_LAST
    learned = "Q_M_THST_HOVER" if q else "MOT_THST_HOVER"
    attrs: dict[str, Attr[Any]] = {
        "frame": _frame(p, vehicle),
        "motor_protocol": Attr(p.label(pwm_name), pwm_name),
        "bidirectional_dshot": Attr(
            dshot and p.on("SERVO_BLH_BDMASK"), f"{pwm_name}, SERVO_BLH_BDMASK"
        ),
        "notch_mode": Attr(
            p.label("INS_HNTCH_MODE") if p.on("INS_HNTCH_ENABLE") else "off",
            "INS_HNTCH_ENABLE, INS_HNTCH_MODE",
        ),
        "capacity_mah": Attr(p.get("BATT_CAPACITY"), "BATT_CAPACITY"),
        "learned_hover_throttle": Attr(p.get(learned), learned),
    }

    def record(a: Attr[Any]) -> dict[str, Any]:
        v: Any = a.value
        value = (
            dataclasses.asdict(v) if dataclasses.is_dataclass(v) and not isinstance(v, type) else v
        )
        return {"value": value, "source": a.source, "note": a.note}

    # Only the simulator has SIM_ parameters, as a log's board says "SITL".
    board: dict[str, str] = (
        {"hal": "SITL"} if any(n.startswith("SIM_") for n in snapshot.values) else {}
    )
    out: dict[str, Any] = {"vehicle": vehicle, "firmware": snapshot.firmware, "board": board}
    return out | {name: record(a) for name, a in attrs.items()}
