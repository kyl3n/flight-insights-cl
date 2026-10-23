"""Buses and interfaces (spec: Bus, interface and processor health; Reference range
library, "Processor, buses and environment").

- Serial ports: incoming data each port had to drop (UART.RxDp), named by the port's
  protocol from SERIALn_PROTOCOL.
- Error counters: CAN bus statistics (CANS), the IO co-processor link (IOMC) and the
  IMU drivers' gyro and accelerometer error counts (IMU.EG, IMU.EA). Errors that keep
  coming through the flight, a CAN bus-off, or ESC and servo commands failing to
  send are out of range; isolated errors are a caution.
- GPS link: gaps between position updates.

Errors that rose before arming are kept apart from those in flight: present from boot
points to wiring or termination, growing with flight to routing or load.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pyarrow as pa

from flight_insights.analyzers.flight import FlightData
from flight_insights.analyzers.model import (
    SEVERITY_OF_ZONE,
    Analyzer,
    Evidence,
    Finding,
    MetricValue,
    Result,
)
from flight_insights.analyzers.registry import Registry

# Counters per message: (field, what it counts). All count up from boot.
COUNTERS: Mapping[str, Sequence[tuple[str, str]]] = {
    "CANS": (
        ("Trej", "transmit rejects"), ("Tov", "transmit overflows"), ("Tto", "transmit timeouts"),
        ("Tab", "transmit aborts"), ("Rov", "receive overflows"), ("Rer", "receive errors"),
        ("Bo", "bus-off"), ("Ftx", "ESC or servo sends failed"),
    ),
    "IOMC": (
        ("Nerr", "protocol failures"), ("Nerr2", "failures on the IO side"),
        ("NDel", "delayed packets"),
    ),
    "IMU": (("EG", "gyro errors"), ("EA", "accelerometer errors")),
}  # fmt: skip
INSTANCE = {"CANS": "I", "IOMC": None, "IMU": "I"}
LABEL = {"CANS": "CAN bus", "IOMC": "IO co-processor", "IMU": "IMU"}
SEVERE = {("CANS", "Bo"), ("CANS", "Ftx")}  # out of range at the first one
GROWING_WINDOWS = 3  # errors in this many separate stretches of the flight are growing
WINDOW_US = 10_000_000
GPS_GAP = 3.0  # a gap of this many usual update intervals


def _times(t: pa.Table) -> np.ndarray[tuple[int], np.dtype[np.int64]]:
    return np.asarray(t.column("TimeUS").to_numpy(), np.int64)


def _protocol(flight: FlightData, reg: Registry, port: int) -> str:
    value = flight.params.get(f"SERIAL{port}_PROTOCOL")
    if value is None:
        return "unknown"
    vehicle = flight.vehicle or "copter"
    meta = reg.pack.metadata(reg.pack.choose(flight.firmware or "0").version, vehicle)
    names: Mapping[str, str] = meta.get(f"SERIAL{port}_PROTOCOL", {}).get("values", {})
    return names.get(str(int(value)), f"protocol {int(value)}")


def _serial(flight: FlightData, reg: Registry, out: Result) -> None:
    uart = flight.table("UART")
    if uart is None or uart.num_rows == 0 or "RxDp" not in uart.column_names:
        return
    t = _times(uart)
    armed = (t >= flight.arm_us) & (t <= flight.disarm_us)
    port = np.asarray(uart.column("I").to_numpy(), np.int64)
    drop = np.asarray(uart.column("RxDp").to_numpy(), np.float64)
    worst = {int(p): float(drop[armed & (port == p)].max()) for p in np.unique(port[armed])}
    if not worst:
        return
    rx = np.asarray(uart.column("Rx").to_numpy(), np.float64) if "Rx" in uart.column_names else None
    ev = Evidence(
        id="bus:serial",
        kind="metric",
        summary="incoming bytes dropped per second, worst per serial port: "
        + ", ".join(f"SERIAL{p} {v:.0f}" for p, v in sorted(worst.items())),
        messages=("UART",),
        fields=("RxDp", "Rx"),
        segment=flight.armed,
        values={
            f"SERIAL{p}": {
                "dropped_bps": round(v, 1),
                "received_bps": round(float(rx[armed & (port == p)].max()), 1)
                if rx is not None
                else None,
                "protocol": _protocol(flight, reg, p),
            }
            for p, v in sorted(worst.items())
        },
    )
    out.evidence.append(ev)
    top = max(worst.values())
    zone = reg.metrics["bus.serial_dropped"].zone(top)
    out.metrics.append(MetricValue("bus.serial_dropped", "flight", top, zone, ev.id))
    for p, v in sorted(worst.items()):
        if reg.metrics["bus.serial_dropped"].zone(v) in ("caution", "out"):
            out.findings.append(
                Finding(
                    code="bus.serial_drops",
                    severity="caution",
                    confidence="high",
                    evidence=(ev,),
                    params={"port": p, "protocol": _protocol(flight, reg, p), "rate": round(v)},
                )
            )


def _rises(t: np.ndarray[Any, Any], count: np.ndarray[Any, Any]) -> list[tuple[int, int]]:
    """(time, increase) wherever a counter went up."""
    step = np.diff(count)
    return [(int(t[k + 1]), int(step[k])) for k in np.flatnonzero(step > 0)]


def _errors(flight: FlightData, reg: Registry, out: Result) -> None:
    events: list[dict[str, Any]] = []
    before: dict[str, int] = {}
    seen = False
    for name, fields in COUNTERS.items():
        table = flight.table(name)
        if table is None or table.num_rows == 0:
            continue
        seen = True
        t = _times(table)
        key = INSTANCE[name]
        inst = (
            np.asarray(table.column(key).to_numpy(), np.int64)
            if key and key in table.column_names
            else np.zeros(len(t), np.int64)
        )
        for field, what in fields:
            if field not in table.column_names:
                continue
            values = np.asarray(table.column(field).to_numpy(), np.int64)
            for i in np.unique(inst):
                sel = inst == i
                where = f"{LABEL[name]} {int(i)}" if key else LABEL[name]
                for at, rise in _rises(t[sel], values[sel]):
                    if at < flight.arm_us:
                        before[f"{where}: {what}"] = before.get(f"{where}: {what}", 0) + rise
                    elif at <= flight.disarm_us:
                        events.append({"source": where, "what": what, "message": name,
                                       "field": field, "at_us": at, "count": rise})  # fmt: skip
    if not seen:
        return
    total = sum(e["count"] for e in events)
    windows = {(e["at_us"] - flight.arm_us) // WINDOW_US for e in events}
    by_source: dict[str, int] = {}
    for e in events:
        label = f"{e['source']}: {e['what']}"
        by_source[label] = by_source.get(label, 0) + e["count"]
    ev = Evidence(
        id="bus:errors",
        kind="fact",
        summary=(
            f"{total} bus and sensor errors in flight"
            + (": " + "; ".join(f"{k} +{v}" for k, v in by_source.items()) if by_source else "")
            + (f"; before arming: {sum(before.values())}" if before else "")
        ),
        messages=tuple(n for n in COUNTERS if flight.has(n)),
        segment=flight.armed,
        values={"in_flight": by_source, "before_arming": before, "events": events[:50]},
    )
    out.evidence.append(ev)
    zone = reg.metrics["bus.errors"].zone(float(total))
    out.metrics.append(MetricValue("bus.errors", "flight", float(total), zone, ev.id))
    if zone not in ("caution", "out"):
        return
    severe = any((e["message"], e["field"]) in SEVERE for e in events)
    growing = len(windows) >= GROWING_WINDOWS
    out.findings.append(
        Finding(
            code="bus.errors",
            severity="out" if severe or growing else SEVERITY_OF_ZONE[zone],
            confidence="high",
            evidence=(ev,),
            params={
                "count": total,
                "sources": "; ".join(f"{k} +{v}" for k, v in by_source.items()),
                "pattern": "growing through the flight" if growing else "isolated",
            },
        )
    )


def _gps(flight: FlightData, reg: Registry, out: Result) -> None:
    gps = flight.table("GPS", ["TimeUS", "I"])
    if gps is None or gps.num_rows < 3:
        return
    t = _times(gps)
    inst = np.asarray(gps.column("I").to_numpy(), np.int64)
    worst, usual_ms = 0.0, 0.0
    for i in np.unique(inst):
        ti = t[(inst == i) & (t >= flight.arm_us) & (t <= flight.disarm_us)]
        if len(ti) < 3:
            continue
        step = np.diff(ti)
        usual = float(np.median(step))
        gap = float(step.max())
        if gap > worst:
            worst, usual_ms = gap, usual / 1000
    if not worst:
        return
    seconds = worst / 1e6
    ev = Evidence(
        id="bus:gps",
        kind="metric",
        summary=f"longest gap between GPS updates {seconds:.2f} s (usually {usual_ms:.0f} ms)",
        messages=("GPS",),
        fields=("TimeUS",),
        segment=flight.armed,
        values={"longest_gap_s": round(seconds, 3), "usual_ms": round(usual_ms, 1)},
    )
    out.evidence.append(ev)
    zone = reg.metrics["bus.gps_gap"].zone(seconds)
    out.metrics.append(MetricValue("bus.gps_gap", "flight", seconds, zone, ev.id))
    if zone in ("caution", "out") and worst > GPS_GAP * usual_ms * 1000:
        out.findings.append(
            Finding(
                code="bus.gps_gaps",
                severity=SEVERITY_OF_ZONE[zone],
                confidence="high",
                evidence=(ev,),
                params={"seconds": round(seconds, 1)},
            )
        )


def _buses(flight: FlightData, reg: Registry) -> Result:
    out = Result()
    _serial(flight, reg, out)
    _errors(flight, reg, out)
    _gps(flight, reg, out)
    if not out.metrics:
        out.skipped = "the log has no serial, CAN, IO co-processor, IMU or GPS statistics"
    return out


BUSES = Analyzer(
    name="buses",
    version=1,
    run=_buses,
    metrics=("bus.serial_dropped", "bus.errors", "bus.gps_gap"),
    findings=("bus.serial_drops", "bus.errors", "bus.gps_gaps"),
)
