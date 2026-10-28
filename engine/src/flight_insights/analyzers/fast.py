"""The fastest steady flight, and how the aircraft held up in it (spec: Power and efficiency
analysis, "Range, endurance and speed", "The speed build-up"; rules in
`knowledge/rules/energy.json` "fast").

What a raise of the cruise or top speed is judged by: the flight's own fastest stretch,
watched for what gives first when a multirotor is pushed.

- **The fast segment.** The flight's level steady tiles (`analyzers/steady.py`) at the
  rules' share (90%) of its fastest one, through the air: each tile's ground velocity less
  the flight's wind, from the drag the flight measures itself (`analyzers/wind.py`). A
  flight that cannot measure its drag is read by ground speed, and says so. It needs a
  real top speed and enough seconds to count.
- **Watched in it:**
  - vibration (VIBE, the worst axis of any IMU) and the accelerometers' clipping;
  - the motors' highest output and the share of time any motor is at its top (RCOU, the
    same range as `analyzers/motors_health.py`);
  - the lean as a share of the position controller's limit (ATT; `PSC_ANGLE_MAX`, else
    `ATC_ANGLE_MAX`);
  - how far the attitude strays from its target (ATT) and the velocity from its target
    (PSCN, PSCE: their target and achieved velocities only, never a position);
  - the most height lost below the target (CTUN, height above home);
  - the packs' highest current and the most the first pack's voltage sags under load
    (BAT: `VoltR` is its estimate at rest);
  - the ESCs' highest temperature (ESC);
  - thrust loss: the firmware's own check (ERR `THRUST_LOSS_CHECK`,
    `ArduCopter/crash_check.cpp`), raised while level at 90% throttle or more and
    descending.

Only velocities, attitudes, heights above home or take-off, outputs and the packs'
readings are read; speeds, shares and counts leave.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers import steady, wind
from flight_insights.analyzers.battery_power import supply
from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import Analyzer, Evidence, MetricValue, Result, Segment
from flight_insights.analyzers.motors_health import motors
from flight_insights.analyzers.registry import Registry
from flight_insights.dataflash import si

Array = np.ndarray[Any, Any]
Spans = list[tuple[int, int]]


def _times(t: pa.Table) -> Array:
    return np.asarray(t.column("TimeUS").to_numpy(), np.int64)


def _inside(times: Array, spans: Spans) -> Array:
    rows = np.zeros(len(times), bool)
    for a, b in spans:
        rows |= (times >= a) & (times <= b)
    return rows


def _merge(start: Array, end: Array) -> Spans:
    """Back-to-back tiles as one span each."""
    out: Spans = []
    for a, b in zip(start.tolist(), end.tolist(), strict=True):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def flight_wind(e: Mapping[str, Array], flight: FlightData, rules: Mapping[str, Any]
                ) -> Array | None:  # fmt: skip
    """The flight's mean wind (north, east, where the air goes) as the wind analyzer
    measures it from the flight's own drag, or None when it cannot."""
    m = wind.measure(wind.windows(e, flight.arm_us, flight.disarm_us, rules), rules)
    return None if m is None else np.mean(m.winds, axis=0)


def _fast(flight: FlightData, reg: Registry) -> Result:
    rules: Mapping[str, Any] = reg.rules["energy"]["fast"]
    if flight.vehicle != "copter" or flight.fingerprint.get("vehicle_class") not in (
        None, "multirotor",
    ):  # fmt: skip
        return Result(skipped="the fast segment is read for multirotors")
    e = wind.ekf(flight)
    if e is None:
        return Result(skipped="no estimator velocity (XKF1) in the log")
    phase_rules: Mapping[str, Any] = reg.rules["energy"]["phases"]
    found = steady.tiles(e, flight.arm_us, flight.disarm_us, float(phase_rules["tile_s"]),
                         int(phase_rules["min_samples"]))  # fmt: skip
    level = found.take(steady.phases(found, phase_rules) == "level")
    if not len(level):
        return Result(skipped="the flight holds no level steady flight")
    blowing = flight_wind(e, flight, reg.rules["wind"])
    through = level.velocity[:, :2] - (blowing if blowing is not None else np.zeros(2))
    speed = np.hypot(through[:, 0], through[:, 1])
    top = float(np.max(speed))
    if top < float(rules["min_top_ms"]):
        return Result(
            skipped=f"the flight's fastest level flight is under {rules['min_top_ms']} m/s"
        )
    fast = speed >= float(rules["share"]) * top
    spans = _merge(level.start_us[fast], level.end_us[fast])
    seconds = float(np.sum(level.end_us[fast] - level.start_us[fast])) / 1e6
    if seconds < float(rules["min_seconds"]):
        return Result(skipped=f"under {rules['min_seconds']} s of flight near its fastest")
    seg = Segment("fast", spans[0][0], spans[-1][1])
    out = Result()

    def add(eid: str, summary: str, messages: tuple[str, ...], fields: tuple[str, ...],
            values: Mapping[str, Any], metrics: Mapping[str, float]) -> None:  # fmt: skip
        out.evidence.append(Evidence(eid, "series", summary, messages, fields, seg, values))
        for mid, v in metrics.items():
            out.metrics.append(MetricValue(mid, "fast", v, reg.metrics[mid].zone(v), eid))

    air = blowing is not None
    mean = float(np.average(speed[fast], weights=(level.end_us - level.start_us)[fast]))
    add("fast:segment",
        f"{seconds:.0f} s at {mean:.1f} m/s {'airspeed' if air else 'ground speed'}, within"
        f" {100 * (1 - float(rules['share'])):.0f}% of the fastest level flight ({top:.1f} m/s)",
        ("XKF1",), wind.EKF_FIELDS,
        {"tiles": int(np.count_nonzero(fast)), "spans": len(spans), "seconds": round(seconds, 1),
         "speed_ms": round(mean, 2), "top_ms": round(top, 2),
         "speed_from": "air" if air else "ground",
         "wind_ms": round(float(np.hypot(*blowing)), 2) if blowing is not None else None},
        {"fast.speed_ms": mean, "fast.seconds": seconds})  # fmt: skip
    _vibration(flight, spans, rules, add)
    _outputs(flight, spans, rules, add)
    _attitude(flight, spans, rules, reg, add)
    _velocity(flight, spans, add)
    _height(flight, spans, add)
    _battery(flight, spans, reg, add)
    _esc(flight, spans, add)
    _thrust_loss(flight, spans, reg, add)
    return out


def _vibration(flight: FlightData, spans: Spans, rules: Mapping[str, Any], add: Any) -> None:
    vibe = flight.table("VIBE")
    if vibe is None or not vibe.num_rows:
        return
    t = _times(vibe)
    rows = _inside(t, spans)
    if not rows.any():
        return
    axes = np.column_stack([si.values(vibe, a) for a in ("VibeX", "VibeY", "VibeZ")])[rows]
    level = float(np.percentile(np.max(axes, axis=1), float(rules["vibe_percentile"])))
    metrics = {"fast.vibe_p95": level}
    values: dict[str, Any] = {"p95": round(level, 2)}
    if "Clip" in vibe.column_names:
        imu = np.asarray(vibe.column("IMU").to_numpy(), np.int64)
        clip = np.asarray(vibe.column("Clip").to_numpy(), np.float64)
        count = 0.0
        for a, b in spans:  # the count is cumulative per IMU
            for i in np.unique(imu):
                r = (imu == i) & (t >= a) & (t <= b)
                if r.any():
                    count += float(np.max(clip[r]) - np.min(clip[r]))
        metrics["fast.clips"] = count
        values["clips"] = int(count)
    add("fast:vibration", f"vibration {level:.1f} m/s/s (95th percentile, worst axis)",
        ("VIBE",), ("VibeX", "VibeY", "VibeZ", "Clip"), values, metrics)  # fmt: skip


def _outputs(flight: FlightData, spans: Spans, rules: Mapping[str, Any], add: Any) -> None:
    ms = motors(flight.params)
    names = [f"C{m.channel}" for m in ms]
    rcou = flight.table("RCOU")
    if not ms or rcou is None or not set(names) <= set(rcou.column_names):
        return
    low = float(flight.params.get("MOT_PWM_MIN", 0)) or float(flight.params.get("RC3_MIN", 1000))
    high = float(flight.params.get("MOT_PWM_MAX", 0)) or float(flight.params.get("RC3_MAX", 2000))
    if high <= low:
        return
    rows = _inside(_times(rcou), spans)
    if not rows.any():
        return
    pwm = np.column_stack([np.asarray(rcou.column(n).to_numpy(), np.float64) for n in names])
    share = (pwm[rows] - low) / (high - low)
    top = float(flight.params.get("MOT_SPIN_MAX", 0.95))
    highest = float(np.percentile(np.max(share, axis=1), float(rules["output_percentile"])))
    at_top = 100.0 * float(np.mean(np.any(share >= top - 0.01, axis=1)))
    add("fast:motors", f"highest motor output {100 * highest:.0f}% of the range",
        ("RCOU",), tuple(names),
        {"highest_pct": round(100 * highest, 1), "at_top_pct": round(at_top, 2), "spin_max": top},
        {"fast.output_max": 100 * highest, "fast.saturation_pct": at_top})  # fmt: skip


def _attitude(flight: FlightData, spans: Spans, rules: Mapping[str, Any], reg: Registry,
              add: Any) -> None:  # fmt: skip
    att = flight.table("ATT", ["TimeUS", "DesRoll", "Roll", "DesPitch", "Pitch"])
    if att is None or not att.num_rows:
        return
    rows = _inside(_times(att), spans)
    if not rows.any():
        return
    roll, pitch = (
        np.radians(si.values(att, "Roll")[rows]),
        np.radians(si.values(att, "Pitch")[rows]),
    )
    tilt = np.degrees(np.arccos(np.clip(np.cos(roll) * np.cos(pitch), -1.0, 1.0)))
    limit = wind.configured_lean(flight.params, reg.rules["wind"]["envelope"])
    lean = float(np.percentile(tilt, float(rules["lean_percentile"])))
    err_r = si.values(att, "DesRoll")[rows] - si.values(att, "Roll")[rows]
    err_p = si.values(att, "DesPitch")[rows] - si.values(att, "Pitch")[rows]
    error = float(np.sqrt(np.mean(err_r**2 + err_p**2)))
    add("fast:attitude", f"leaning {lean:.0f} deg of a {limit:.0f} deg limit",
        ("ATT",), ("DesRoll", "Roll", "DesPitch", "Pitch"),
        {"lean_deg": round(lean, 1), "limit_deg": limit, "error_deg": round(error, 2)},
        {"fast.lean_share": 100 * lean / limit, "fast.attitude_error_deg": error})  # fmt: skip


def _velocity(flight: FlightData, spans: Spans, add: Any) -> None:
    north = flight.table("PSCN", ["TimeUS", "TVN", "VN"])
    east = flight.table("PSCE", ["TimeUS", "TVE", "VE"])
    if north is None or east is None or not north.num_rows or north.num_rows != east.num_rows:
        return
    if not {"TVN", "VN"} <= set(north.column_names) or not {"TVE", "VE"} <= set(east.column_names):
        return
    rows = _inside(_times(north), spans)
    if not rows.any():
        return
    dn = si.values(north, "TVN")[rows] - si.values(north, "VN")[rows]
    de = si.values(east, "TVE")[rows] - si.values(east, "VE")[rows]
    error = float(np.sqrt(np.mean(dn**2 + de**2)))
    add("fast:velocity", f"velocity {error:.2f} m/s from its target (RMS)",
        ("PSCN", "PSCE"), ("TVN", "VN", "TVE", "VE"), {"error_ms": round(error, 3)},
        {"fast.velocity_error_ms": error})  # fmt: skip


def _height(flight: FlightData, spans: Spans, add: Any) -> None:
    ctun = flight.table("CTUN", ["TimeUS", "DAlt", "Alt"])
    if ctun is None or not ctun.num_rows or not {"DAlt", "Alt"} <= set(ctun.column_names):
        return
    rows = _inside(_times(ctun), spans)
    if not rows.any():
        return
    below = si.values(ctun, "DAlt")[rows] - si.values(ctun, "Alt")[rows]
    lost = max(float(np.max(below)), 0.0)
    add(
        "fast:height",
        f"at most {lost:.1f} m below the target height",
        ("CTUN",),
        ("DAlt", "Alt"),
        {"lost_m": round(lost, 2)},
        {"fast.height_loss_m": lost},
    )


def _battery(flight: FlightData, spans: Spans, reg: Registry, add: Any) -> None:
    packs = supply(flight, reg.rules["batteries"])
    if packs is None:
        return
    rows = _inside(packs.t, spans)
    if not rows.any():
        return
    amps = float(np.max(packs.current[rows]))
    metrics = {"fast.current_max_a": amps}
    values: dict[str, Any] = {"current_a": round(amps, 1), "monitors": len(packs.instances)}
    bat = flight.table("BAT", ["TimeUS", "Inst", "Volt", "VoltR"])
    if bat is not None and bat.num_rows and "VoltR" in bat.column_names:
        t = _times(bat)
        inst = (np.asarray(bat.column("Inst").to_numpy(), np.int64)
                if "Inst" in bat.column_names else np.zeros(len(t), np.int64))  # fmt: skip
        r = _inside(t, spans) & (inst == packs.instances[0])
        if r.any():
            sag = max(float(np.max(si.values(bat, "VoltR")[r] - si.values(bat, "Volt")[r])), 0.0)
            metrics["fast.sag_v"] = sag
            values["sag_v"] = round(sag, 2)
    add("fast:battery", f"the packs gave up to {amps:.0f} A", ("BAT",),
        ("Volt", "VoltR", "Curr"), values, metrics)  # fmt: skip


def _esc(flight: FlightData, spans: Spans, add: Any) -> None:
    if flight.simulated:  # the simulator's ESC temperatures are placeholders
        return
    esc = flight.table("ESC", ["TimeUS", "Instance", "Temp"])
    if esc is None or not esc.num_rows or "Temp" not in esc.column_names:
        return
    rows = _inside(_times(esc), spans)
    if not rows.any():
        return
    hottest = float(np.max(si.values(esc, "Temp")[rows]))
    add("fast:esc", f"the hottest ESC reached {hottest:.0f} C", ("ESC",), ("Temp",),
        {"temp_c": round(hottest, 1)}, {"fast.esc_temp_max_c": hottest})  # fmt: skip


def _thrust_loss(flight: FlightData, spans: Spans, reg: Registry, add: Any) -> None:
    names: Mapping[str, str] = reg.names(flight.firmware).get("error_subsystems", {})
    code = next((int(k) for k, v in names.items() if v == "THRUST_LOSS_CHECK"), None)
    if code is None:
        return
    err = flight.table("ERR", ["TimeUS", "Subsys", "ECode"])
    count = 0
    if err is not None and err.num_rows:
        t = _times(err)
        sub = np.asarray(err.column("Subsys").to_numpy(), np.int64)
        ecode = np.asarray(err.column("ECode").to_numpy(), np.int64)
        count = int(np.count_nonzero(_inside(t, spans) & (sub == code) & (ecode != 0)))
    add(
        "fast:thrust",
        f"{count} thrust-loss warnings near top speed",
        ("ERR",),
        ("Subsys", "ECode"),
        {"warnings": count},
        {"fast.thrust_loss": float(count)},
    )


FAST = Analyzer(
    name="fast",
    version=1,
    run=_fast,
    metrics=(
        "fast.speed_ms",
        "fast.seconds",
        "fast.vibe_p95",
        "fast.clips",
        "fast.output_max",
        "fast.saturation_pct",
        "fast.lean_share",
        "fast.attitude_error_deg",
        "fast.velocity_error_ms",
        "fast.height_loss_m",
        "fast.current_max_a",
        "fast.sag_v",
        "fast.esc_temp_max_c",
        "fast.thrust_loss",
    ),
    findings=(),
)
