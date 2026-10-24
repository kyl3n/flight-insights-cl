"""The engine's location scrubber: the shared corpus of clean and leaky packets (which the
shell's own check reads too), every detector, the tripwire, and that a refusal never
repeats the value it refused."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest

from flight_insights.ai.policy import Policy
from flight_insights.ai.schema import PACKET, validate
from flight_insights.ai.scrubber import ScrubError, check, scrub
from flight_insights.ai.tripwire import flight_points
from flight_insights.knowledge import load_pack
from flight_insights.store import ColumnCache, StoreKeys

CORPUS = Path(__file__).resolve().parents[2] / "tests/scrubber"
POLICY = Policy.load(load_pack(), "4.7.2")
POINTS: list[tuple[float, float]] = [
    (float(a), float(b)) for a, b in json.loads((CORPUS / "tripwire.json").read_text())["points"]
]


def corpus(kind: str) -> list[tuple[str, dict[str, Any]]]:
    files = sorted((CORPUS / "packets" / kind).glob("*.json"))
    assert files, f"no {kind} packets in the corpus"
    return [(f.stem, json.loads(f.read_text(encoding="utf-8"))["packet"]) for f in files]


@pytest.mark.parametrize(("name", "packet"), corpus("clean"))
def test_clean_packets_leave(name: str, packet: dict[str, Any]) -> None:
    assert check(packet, POINTS, POLICY) == [], name
    assert scrub(packet, POINTS, POLICY) is packet


@pytest.mark.parametrize(("name", "packet"), corpus("leaky"))
def test_leaky_packets_are_refused(name: str, packet: dict[str, Any]) -> None:
    with pytest.raises(ScrubError) as refused:
        scrub(packet, POINTS, POLICY)
    for v in refused.value.violations:
        text = f"{v.path} {v.rule}"
        # A refusal names where and why, never the value: it can be shown and logged.
        for lat, lon in POINTS:
            for value in (lat, lon):
                for shown in (f"{value:.2f}", f"{value:.4f}", str(round(value * 1e7))):
                    assert shown not in text, (name, text)


def test_every_array_and_text_in_the_schema_is_bounded() -> None:
    def walk(s: Any, path: str) -> None:
        if isinstance(s, dict):
            node = cast(dict[str, Any], s)
            raw: Any = node.get("type", [])
            kinds = [raw] if isinstance(raw, str) else cast(list[str], raw)
            if "array" in kinds:
                assert "maxItems" in node, path
            if "object" in kinds and "patternProperties" in node:
                assert node.get("additionalProperties") is False and "maxProperties" in node, path
            if "object" in kinds and "properties" in node:
                assert node.get("additionalProperties") is False, path
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(s, list):
            for i, v in enumerate(cast(list[Any], s)):
                walk(v, f"{path}[{i}]")

    walk(PACKET, "$")


def minimal() -> dict[str, Any]:
    return {
        "schema": "fi.ai.packet/1",
        "purpose": "chat",
        "aircraft": {"vehicle": "plane", "firmware": "4.7.2"},
    }


def test_the_shape_check_names_the_place_and_never_the_value() -> None:
    p = minimal() | {"metrics": [{"metric": "vibration.p95", "segment": "hover", "value": "47.1"}]}
    assert validate(p) == ["$.metrics[0].value: wrong type"]
    p = minimal() | {"aircraft": {"vehicle": "rover", "firmware": "4.7.2"}}
    assert validate(p) == ["$.aircraft.vehicle: not one of the allowed values"]
    assert validate({"schema": "fi.ai.packet/1"}) == ["$: missing purpose", "$: missing aircraft"]
    assert validate(minimal() | {"parameters": {"atc_rat": 1.0}}) == [
        "$.parameters: a key the packet may not hold"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "ESC peak temperatures 52, 53, 66 °C",
        "1,950 W at 90.1 A in hover over 312.5 s",
        "93.5° of lag at 20 Hz; thrust 5 N",
        "SERIAL2 (MAVLink2) dropped up to 120 bytes/s",
        "the notch sat at 201.8 Hz, 12.4 Hz from the motor peak",
        "Motor 3: RPM collapsed while still commanded, first at 41.2 s",
        "board CubeOrangePlus, STM32H757",
    ],
)
def test_engine_wording_passes_the_detectors(text: str) -> None:
    p = minimal() | {"evidence": [{"ref": "E1", "kind": "fact", "summary": text}]}
    assert check(p, POINTS, POLICY) == [], text


def test_number_rules() -> None:
    def refused(value: float) -> bool:
        p = minimal() | {"metrics": [{"metric": "m.x", "segment": "flight", "value": value}]}
        return bool(check(p, [], POLICY))

    assert refused(503_456_789)  # a coordinate x1e7
    assert refused(50.45012)  # five decimals, in the range of degrees
    assert not refused(50.4501) and not refused(0.000612) and not refused(655_358)
    assert not refused(1950.4) and not refused(-31.12)


def test_the_tripwire() -> None:
    lat, lon = POINTS[0]

    def refused(*values: float) -> bool:
        metrics = [{"metric": "m.x", "segment": "flight", "value": v} for v in values]
        return bool(check(minimal() | {"metrics": metrics}, POINTS, POLICY))

    assert refused(round(lat, 4)) and refused(round(lon, 4))  # within ~5 m
    assert refused(round(math.radians(lat), 7))
    assert refused(round(lat, 2), round(lon, 2))  # a pair to 0.01 degrees
    assert not refused(round(lat, 2))  # one value to 0.01 degrees alone gives no place
    assert not refused(round(lat, 2), 99.5)
    # Without the tripwire, only the shapes and detectors guard: a 4-decimal value passes.
    assert not check(
        minimal() | {"metrics": [{"metric": "m.x", "segment": "flight", "value": round(lat, 4)}]},
        [],
        POLICY,
    )


def test_the_tripwire_reads_the_flights_positions(tmp_path: Path) -> None:
    cache = ColumnCache(tmp_path, StoreKeys.generate())

    def meta(unit: str) -> dict[str, str]:
        return {"unit": unit, "position": "1"}

    schema = pa.schema(
        [
            pa.field("TimeUS", pa.uint64(), metadata={"unit": "s"}),
            pa.field("Lat", pa.float64(), metadata=meta("deglatitude")),
            pa.field("Lng", pa.float64(), metadata=meta("deglongitude")),
            pa.field("Spd", pa.float32(), metadata={"unit": "m/s"}),
        ]
    )
    lats = [12.34567, 12.34568, 0.0, 12.35]  # the second is in the first's cell; 0,0 is none
    lons = [65.4321, 65.43211, 0.0, 65.44]
    columns: list[Any] = [
        pa.array([1, 2, 3, 4], pa.uint64()), pa.array(lats), pa.array(lons),
        pa.array([1.0] * 4, pa.float32()),
    ]  # fmt: skip
    table = pa.Table.from_arrays(columns, schema=schema)
    cache.write_table("f1", "GPS", table)
    points = flight_points(cache, "f1")
    assert points == [(12.34567, 65.4321), (12.35, 65.44)]
    assert flight_points(cache, "f1", cap=1) == [(12.34567, 65.4321)]


def test_the_tripwire_is_what_catches_the_tripwire_packets() -> None:
    """Without the flight's positions, the packets that only the tripwire can catch pass:
    the corpus would fail if the tripwire stopped working."""
    for name, packet in corpus("leaky"):
        if name.startswith("tripwire_"):
            assert check(packet, POINTS, POLICY)
            assert check(packet, [], POLICY) == [], name
