"""Plant one known position into every message that can hold one, for the scrubber's
end-to-end corpus (spec: AI layer, "Proof, not promise": a test corpus plants known
coordinates in every message type).

A real SITL fixture log is the carrier, so it imports and analyses like any flight.
Planted messages are spliced in just before the log's first parameter message, at its
timestamp, so time stays monotonic and they fall inside the flight. Every message type
that holds a position in any release's definitions (knowledge/logs, from ArduPilot's own
source) gets one row: with the log's own layout where the log defines the type, else
with the layout the definitions give. Location-revealing fields with no position unit
(absolute altitudes, GNSS week and time), free text and location parameters are planted
too.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from flight_insights.dataflash import read_dataflash
from flight_insights.knowledge import Pack

PLANT_LAT, PLANT_LON = -27.482137, 152.991346  # an arbitrary spot, not any real flight's
PLANT_ALT = 4321.987
PLANT_WEEK, PLANT_TOW = 2391, 345_678_123

PACK_CHARS = {
    "a": "32h", "b": "b", "B": "B", "h": "h", "H": "H", "i": "i", "I": "I", "q": "q",
    "Q": "Q", "f": "f", "d": "d", "n": "4s", "N": "16s", "Z": "64s", "c": "h",
    "C": "H", "e": "i", "E": "I", "L": "i", "M": "B",
}  # fmt: skip
C_TYPES = {
    "float": "f", "double": "d", "uint8_t": "B", "int8_t": "b", "uint16_t": "H",
    "int16_t": "h", "uint32_t": "I", "int32_t": "i", "uint64_t": "Q", "int64_t": "q",
    "int16_t * 100": "c", "uint16_t * 100": "C", "int32_t * 100": "e", "uint32_t * 100": "E",
    "int32_t latitude/longitude": "L", "char[4]": "n", "char[16]": "N", "char[64]": "Z",
    "int16_t[32]": "a", "uint8_t flight mode": "M",
}  # fmt: skip
POSITION_UNITS = ("deglatitude", "deglongitude", "1e-7 deglatitude", "1e-7 deglongitude")
TEXT = f"home at {PLANT_LAT:.6f}, {PLANT_LON:.6f}"
LOCATION_PARAMS = {
    "AHRS_ORIGIN_LAT": PLANT_LAT, "AHRS_ORIGIN_LON": PLANT_LON, "AHRS_ORIGIN_ALT": PLANT_ALT,
    "BCN_LATITUDE": PLANT_LAT, "BCN_LONGITUDE": PLANT_LON,
    "SCR_USER1": PLANT_LAT, "SCR_USER2": PLANT_LON, "SIM_OPOS_LAT": PLANT_LAT,
}  # fmt: skip


def _value(name: str, ch: str, unit: str) -> Any:
    lat = "lat" in unit.lower() or name.lower() in ("lat", "tlat")
    lon = "lon" in unit.lower() or "lng" in name.lower() or name.lower() in ("lon", "tlng")
    coord = PLANT_LAT if lat else PLANT_LON if lon else None
    if ch in "nNZ":
        return TEXT.encode()
    if ch == "a":
        return [0] * 32
    if coord is not None:
        if ch in "Li":
            return round(coord * 1e7)
        if ch in "fd":
            return coord
    if ch in "fd" and ("alt" in name.lower() or name.lower() in ("hgt", "height")):
        return PLANT_ALT
    if name in ("GWk", "Week", "week"):
        return PLANT_WEEK
    if name in ("GMS", "WkMS", "SMS"):
        return PLANT_TOW
    return 0


def _body(fmt: str, columns: list[str], units: list[str], time_us: int) -> bytes:
    values: list[Any] = []
    for ch, name, unit in zip(fmt, columns, units, strict=True):
        v: Any = time_us if name == "TimeUS" else _value(name, ch, unit)
        values.extend(v if ch == "a" else [v])
    return struct.pack("<" + "".join(PACK_CHARS[c] for c in fmt), *values)


def plant(source: Path, target: Path, pack: Pack) -> dict[str, int]:
    """Write `source` with planted rows to `target`; returns message type -> rows planted."""
    data = source.read_bytes()
    planted: dict[str, int] = {}
    with read_dataflash(source) as df:
        formats = df.index.formats
        by_name = {f.name: f for f in formats if df.count_format(formats.index(f))}
        used = {f.type for f in formats}
        parm = df.formats_named("PARM")[0]
        splice = int(df.offsets(parm)[0])
        time_us = int(df.field(parm, "TimeUS")[0])
        units_of = {f.name: list(f.units) for f in formats}
    free = (t for t in range(1, 255) if t not in used and t not in (0x80, 0x81, 0x82, 0x83))
    fmt_fmt = by_name["FMT"]
    out = bytearray()

    def message(type_: int, body: bytes) -> None:
        out.extend(b"\xa3\x95" + bytes([type_]) + body)

    definitions: dict[str, dict[str, Any]] = {}
    for version in sorted(pack.manifest["logs"]):
        definitions |= pack.log_messages(version)
    holders = {
        name for name, m in definitions.items()
        if any(f["units"] in POSITION_UNITS or "latitude/longitude" in f["type"]
               for f in m["fields"].values())
    }  # fmt: skip
    for name in sorted(holders | {"MSG", "BARO", "GPS"}):
        if name in by_name:  # the log's own layout
            f = by_name[name]
            fmt, columns, type_ = f.format, f.columns, f.type
            units = units_of.get(name) or [""] * len(fmt)
        else:  # the release's layout, as a new type
            fields = definitions[name]["fields"]
            columns = list(fields)
            try:
                fmt = "".join(C_TYPES[fields[c]["type"]] for c in columns)
            except KeyError:
                continue
            if len(fmt) > 16 or len(",".join(columns)) > 64:
                continue
            type_ = next(free)
            units = [fields[c]["units"] for c in columns]
            size = 3 + struct.calcsize("<" + "".join(PACK_CHARS[c] for c in fmt))
            message(
                fmt_fmt.type,
                struct.pack("<BB4s16s64s", type_, size, name.encode(), fmt.encode(),
                            ",".join(columns).encode()),
            )  # fmt: skip
        message(type_, _body(fmt, columns, units, time_us))
        planted[name] = 1
    parm_fmt = by_name["PARM"]
    for pname, value in LOCATION_PARAMS.items():
        cols = parm_fmt.columns
        vals: list[Any] = []
        for ch, c in zip(parm_fmt.format, cols, strict=True):
            vals.append(time_us if c == "TimeUS" else pname.encode() if ch in "nNZ" else value)
        message(
            parm_fmt.type, struct.pack("<" + "".join(PACK_CHARS[c] for c in parm_fmt.format), *vals)
        )
    planted["PARM"] = len(LOCATION_PARAMS)
    target.write_bytes(data[:splice] + bytes(out) + data[splice:])
    return planted


def leaks(text: str) -> list[str]:
    """Every encoding of a planted value found in `text`: decimal degrees to three to
    seven places (latitude and longitude together at three, either alone at four or
    more), x1e7 integers, radians, the planted altitude, GNSS week and time of week."""
    import math

    found: list[str] = []
    for places in range(4, 8):
        for value in (PLANT_LAT, PLANT_LON):
            if f"{value:.{places}f}" in text:
                found.append(f"{value:.{places}f}")
    if f"{PLANT_LAT:.3f}" in text and f"{PLANT_LON:.3f}" in text:
        found.append("latitude and longitude to 3 places")
    for value in (PLANT_LAT, PLANT_LON):
        if str(round(value * 1e7)) in text or f"{math.radians(value):.5f}" in text:
            found.append("scaled or radians")
    for token in (f"{PLANT_ALT:.1f}", str(PLANT_TOW)):
        if token in text:
            found.append(token)
    return found
