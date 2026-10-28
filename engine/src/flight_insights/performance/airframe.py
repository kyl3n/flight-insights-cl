"""An airframe's power model from its own flights, kept until they change (spec: Power and
efficiency analysis, "Range, endurance and speed", "The power model"; the
`performance_fit` table, on this laptop only).

- **Its flights.** Every flight of the airframe gives its points (`performance/points.py`)
  or the reason it gives none; a flight whose weight is not known gives none to the fit,
  since every point is weighed in newtons.
- **The rotors.** The discs of its motor frame (`motor_out.motor_frame`; a coaxial pair
  shares its arm's disc, as `sim.export.rotors` counts them) at the setup's propeller
  diameter, from its latest flight.
- **The fit** (`fit.fitted`): the model at the level its data supports, the error of each
  flight's energy predicted without it, and the bootstrap's models.
- **Kept** with what it was made from: the engine and the knowledge, the airframe and its
  setup, and each flight with its latest analysis and weather record. Any change, and it is
  made again on the next request.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, cast

from flight_insights import __version__
from flight_insights.analyzers import FlightData, Registry
from flight_insights.analyzers.flight import FlightError
from flight_insights.analyzers.motor_out import motor_frame
from flight_insights.analyzers.registry import knowledge_version
from flight_insights.knowledge import Pack
from flight_insights.performance.fit import Fitted, G, Model, Row, fitted
from flight_insights.performance.points import FlightPoints, Point, flight_points
from flight_insights.sim.export import rotors
from flight_insights.store.cache import CacheError, ColumnCache
from flight_insights.store.db import Store

INCH = 0.0254
FORMAT = 1  # of model_json: raised when what is kept changes shape


class PerformanceError(LookupError):
    """No such airframe."""


@dataclass(frozen=True)
class AirframeModel:
    airframe_id: str
    fitted: Fitted | None
    disc_area: float | None  # m^2
    coaxial: bool
    flights: tuple[FlightPoints, ...]
    skipped: Mapping[str, str]  # flight id: why it gave no points to the fit
    why: str | None  # why there is no model


def flight_ids(store: Store, airframe_id: str) -> list[str]:
    rows = store.conn.execute(
        "SELECT id FROM flight WHERE airframe_id = ? ORDER BY id", (airframe_id,)
    ).fetchall()
    return [str(r[0]) for r in rows]


def basis(store: Store, pack: Pack, airframe_id: str) -> str:
    """What the airframe's model is made from, as one hash."""
    airframe = store.get("airframe", airframe_id) or {}
    platform = store.get("platform", str(airframe.get("platform_id"))) or {}
    build = store.get("build_version", str(airframe.get("current_build_id"))) or {}
    reference = store.get("build_version", str(platform.get("reference_build_id"))) or {}
    flights = store.conn.execute(
        "SELECT f.id, f.updated_hlc, "
        "(SELECT r.id FROM analysis_run r WHERE r.flight_id = f.id "
        " ORDER BY r.created_hlc DESC LIMIT 1), "
        "(SELECT w.basis FROM flight_weather w WHERE w.flight_id = f.id) "
        "FROM flight f WHERE f.airframe_id = ? ORDER BY f.id",
        (airframe_id,),
    ).fetchall()
    made = {
        "format": FORMAT,
        "engine": __version__,
        "knowledge": knowledge_version(pack),
        "airframe": airframe.get("updated_hlc"),
        "build": [airframe.get("current_build_id"), build.get("updated_hlc")],
        "platform": [platform.get("updated_hlc"), reference.get("updated_hlc")],
        "flights": [list(r) for r in flights],
    }
    return hashlib.sha256(json.dumps(made, sort_keys=True).encode()).hexdigest()


def _rotors(flight: FlightData, reg: Registry) -> tuple[float, bool] | str:
    """The disc area and whether a pair of rotors shares a disc, or why it is not known."""
    found = motor_frame(flight, reg)
    if isinstance(found, str):
        return found
    frame, _name = found
    prop = flight.answers.get("prop_diameter_in")
    if not (isinstance(prop, int | float) and prop > 0):
        return "enter the propeller diameter in the setup: the power model needs the rotors' area"
    motors: list[Mapping[str, Any]] = frame["motors"]
    discs = rotors(motors)
    return discs * math.pi * (float(prop) * INCH / 2.0) ** 2, len(motors) > discs


def rows(flights: tuple[FlightPoints, ...]) -> list[Row]:
    """Every point of every flight whose weight is known, weighed in newtons."""
    return [
        Row(f.flight_id, f.mass_kg * G, p.airspeed, p.climb, p.power, p.density, p.seconds)
        for f in flights
        if f.mass_kg is not None
        for p in f.points
    ]


def build(store: Store, cache: ColumnCache, pack: Pack, airframe_id: str) -> AirframeModel:
    """The airframe's model made afresh from its flights."""
    reg = Registry.load(pack)
    ids = flight_ids(store, airframe_id)
    found: list[FlightPoints] = []
    skipped: dict[str, str] = {}
    for fid in ids:
        p = flight_points(store, cache, pack, fid)
        if isinstance(p, str):
            skipped[fid] = p
        elif p.mass_kg is None:
            skipped[fid] = p.mass_from
            found.append(p)
        else:
            found.append(p)
    flights = tuple(found)

    def none(why: str, disc: float | None = None, coaxial: bool = False) -> AirframeModel:
        return AirframeModel(airframe_id, None, disc, coaxial, flights, skipped, why)

    if not ids:
        return none("the airframe has no flights")
    try:
        latest = FlightData.load(store, cache, pack, ids[-1])
    except (FlightError, CacheError) as e:
        return none(str(e))
    discs = _rotors(latest, reg)
    if isinstance(discs, str):
        return none(discs)
    area, coaxial = discs
    got = rows(flights)
    if not got:
        return none("no flight gave points with a known weight", area, coaxial)
    model = fitted(got, area, coaxial, reg.rules["performance"])
    if model is None:
        return none("too few steady points to fit the power model", area, coaxial)
    return AirframeModel(airframe_id, model, area, coaxial, flights, skipped, None)


def _model(raw: Mapping[str, Any]) -> Model:
    return Model(**{k: raw[k] for k in raw if k != "factors"}, factors=dict(raw["factors"]))


def to_json(m: AirframeModel) -> str:
    return json.dumps(asdict(m), sort_keys=True)


def from_json(text: str) -> AirframeModel:
    raw = cast(dict[str, Any], json.loads(text))
    f = raw["fitted"]
    fit = None
    if f is not None:
        fit = Fitted(
            model=_model(f["model"]), level=f["level"], rows=f["rows"], flights=f["flights"],
            lofo=dict(f["lofo"]), bootstrap={k: (v[0], v[1]) for k, v in f["bootstrap"].items()},
            left_out=f["left_out"], replicates=tuple(_model(r) for r in f["replicates"]),
        )  # fmt: skip
    flights = tuple(
        FlightPoints(
            flight_id=p["flight_id"],
            points=tuple(Point(**x) for x in p["points"]),
            mass_kg=p["mass_kg"],
            mass_from=p["mass_from"],
            wind_from=p["wind_from"],
            density_from=p["density_from"],
            left_out=dict(p["left_out"]),
        )
        for p in raw["flights"]
    )
    return AirframeModel(raw["airframe_id"], fit, raw["disc_area"], raw["coaxial"], flights,
                         dict(raw["skipped"]), raw["why"])  # fmt: skip


def load(store: Store, cache: ColumnCache, pack: Pack, airframe_id: str) -> AirframeModel:
    """The airframe's model: the one kept while what it was made from is unchanged, else
    made again and kept."""
    if store.get("airframe", airframe_id) is None:
        raise PerformanceError("no such airframe")
    b = basis(store, pack, airframe_id)
    kept = store.conn.execute(
        "SELECT basis, model_json FROM performance_fit WHERE airframe_id = ?", (airframe_id,)
    ).fetchone()
    if kept is not None and kept[0] == b:
        return from_json(str(kept[1]))
    made = build(store, cache, pack, airframe_id)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO performance_fit (airframe_id, basis, model_json) VALUES (?, ?, ?) "
            "ON CONFLICT (airframe_id) DO UPDATE SET basis = excluded.basis, "
            "model_json = excluded.model_json",
            (airframe_id, b, to_json(made)),
        )
    return made
