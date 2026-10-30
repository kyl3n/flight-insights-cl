"""Makers' bench tables and two builds side by side (spec: Power and efficiency analysis,
"Range, endurance and speed", "What the user gets"; the mock-up's "Build against build").

A bench table is a maker's test of one motor with one prop at one voltage: thrust and
electrical power at each throttle step, and where the maker prints them, each step's
speed, torque, current and voltage, which the propeller and motor model reads (spec:
"Propeller and motor choice"). A person enters them for a platform, marking one
set as the parts it flies (`current`) and one as the parts it may fly (`candidate`); the
app never fetches or invents them.

- **The candidate build** is the airframe's own fitted model with its propulsion power
  (induced, climb and parasite, profile) scaled by the ratio of the two tables' power at
  the thrust each motor gives in hover at that weight. Its drag, packs and avionics are
  the airframe's own, measured. A heavier or lighter motor changes the take-off weight
  by the motors' count times the difference, when both tables give a motor's mass.
- **The ratio is read only where both tables were measured.** A hover thrust past
  either table's last step, or below its first, gives no candidate figures: the screen
  says which table runs out, rather than extrapolating a maker's curve.
- **What it cannot see.** A bench measures a motor alone in still air: coaxial losses,
  arm blockage and forward flight are the airframe's own and come from its flights. Two
  tables on different props or voltages compare those setups, not the motor alone; the
  comparison says so. Every candidate figure is an estimate, not flown.
- **Read in the same air.** A maker states the test's ambient temperature; thinner, hotter
  air needs more power for the same thrust. With a fixed-pitch prop at a given thrust, the
  induced and profile power both go as one over the square root of the air density, so a
  table with its temperature is brought to the air asked for (at standard pressure: no
  maker gives the pressure; the motor's own losses are taken to scale with the rest). Two
  tables are read in the same air only when both give their temperature; otherwise the
  comparison says one is missing.
- **Flown against the bench.** The airframe's hover power per motor against its current
  table's at the same thrust: well above the bench means losses outside the motor (prop
  wash on arms, coaxial interference, damaged props), as the spec's g/W check reads it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any

from flight_insights.performance.fit import Model
from flight_insights.store.db import Store

ROLES = ("current", "candidate", "none")
R_AIR = 287.05287  # J/(kg K), dry air
P_STANDARD = 101325.0  # Pa
AMBIENT_C = (-40.0, 60.0)  # the temperatures a bench is plausibly run in


def test_density(ambient_c: float) -> float:
    """The air the bench ran in, at standard pressure."""
    return P_STANDARD / (R_AIR * (ambient_c + 273.15))


class BenchError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    throttle_pct: float
    thrust_g: float
    power_w: float
    rpm: float | None = None
    torque_nm: float | None = None
    current_a: float | None = None
    volts: float | None = None

    def view(self) -> dict[str, float]:
        """The step's values, leaving out the columns the maker did not print."""
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @property
    def shaft_w(self) -> float | None:
        """The power the motor gives the prop, where the table has speed and torque."""
        if self.rpm is None or self.torque_nm is None:
            return None
        return self.torque_nm * self.rpm * 2 * math.pi / 60


EXTRA = ("rpm", "torque_nm", "current_a", "volts")  # the columns a maker may also print


@dataclass(frozen=True)
class Bench:
    id: str
    version: int
    role: str
    label: str
    motor: str | None
    kv: float | None
    prop_diameter_in: float | None
    prop_pitch_in: float | None
    volts: float | None
    motor_mass_g: float | None
    source: str | None
    rows: tuple[Row, ...]
    ambient_c: float | None = None

    def power_at(self, thrust_g: float) -> float | None:
        """Electrical power for this thrust, read between the steps on a log-log scale
        (power grows about as thrust to the 1.5); None outside the steps measured."""
        t = [r.thrust_g for r in self.rows]
        if not t[0] - 1e-9 <= thrust_g <= t[-1] + 1e-9:
            return None
        for a, b in pairwise(self.rows):
            if a.thrust_g <= thrust_g <= b.thrust_g:
                f = math.log(thrust_g / a.thrust_g) / math.log(b.thrust_g / a.thrust_g)
                return math.exp(math.log(a.power_w) + f * math.log(b.power_w / a.power_w))
        return self.rows[0].power_w

    def power_in(self, thrust_g: float, density: float) -> float | None:
        """The power for this thrust in air of this density, when the table gives the
        temperature it ran in; as measured otherwise."""
        at = self.power_at(thrust_g)
        if at is None or self.ambient_c is None:
            return at
        return at * math.sqrt(test_density(self.ambient_c) / density)

    @property
    def max_thrust_g(self) -> float:
        return self.rows[-1].thrust_g

    @property
    def full_throttle(self) -> bool:
        """Whether the table reaches full throttle; otherwise its last step is the most
        it was measured to give, not the most the motor gives."""
        return self.rows[-1].throttle_pct >= 99.0

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "version": self.version, "role": self.role, "label": self.label,
            "motor": self.motor, "kv": self.kv, "prop_diameter_in": self.prop_diameter_in,
            "prop_pitch_in": self.prop_pitch_in, "volts": self.volts,
            "motor_mass_g": self.motor_mass_g, "source": self.source, "ambient_c": self.ambient_c,
            "rows": [r.view() for r in self.rows],
            "max_thrust_g": self.max_thrust_g, "full_throttle": self.full_throttle,
        }  # fmt: skip


def _row(r: Mapping[str, Any]) -> Row:
    extra: dict[str, float | None] = {}
    for key in EXTRA:
        v = r.get(key)
        extra[key] = None if v is None or v == "" else float(v)
    return Row(float(r["throttle_pct"]), float(r["thrust_g"]), float(r["power_w"]), **extra)


def check_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Row, ...]:
    """A table's steps, checked: at least three, throttle within 0-100 %, thrust and
    power positive, and both rising with throttle. A step's speed, torque, current and
    voltage, where given, are above zero, and the shaft power its speed and torque make
    stays below the electrical power it draws (no motor gives more than it takes)."""
    out: list[Row] = []
    for i, r in enumerate(rows):
        try:
            row = _row(r)
        except (KeyError, TypeError, ValueError):
            raise BenchError(f"step {i + 1} needs throttle %, thrust in g and power in W") from None
        if not 0 < row.throttle_pct <= 100:
            raise BenchError(f"step {i + 1}: throttle is a percentage, above 0 and up to 100")
        if row.thrust_g <= 0 or row.power_w <= 0:
            raise BenchError(f"step {i + 1}: thrust and power are above zero")
        if any(v is not None and v <= 0 for v in (row.rpm, row.torque_nm, row.current_a,
                                                 row.volts)):  # fmt: skip
            raise BenchError(f"step {i + 1}: speed, torque, current and voltage are above zero")
        shaft = row.shaft_w
        if shaft is not None and shaft >= row.power_w:
            raise BenchError(f"step {i + 1}: torque times speed is more than the power drawn; "
                             "check the torque's unit (N·m)")  # fmt: skip
        out.append(row)
    if len(out) < 3:
        raise BenchError("a table needs at least three steps")
    out.sort(key=lambda r: r.throttle_pct)
    for a, b in pairwise(out):
        if not (b.throttle_pct > a.throttle_pct and b.thrust_g > a.thrust_g
                and b.power_w > a.power_w):  # fmt: skip
            raise BenchError("thrust and power rise with throttle, one step per throttle")
    return tuple(out)


def _number(values: Mapping[str, Any], key: str) -> float | None:
    v = values.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or v <= 0:
        raise BenchError(f"{key} is a number above zero")
    return float(v)


def _temperature(values: Mapping[str, Any]) -> float | None:
    v = values.get("ambient_c")
    if v is None or v == "":
        return None
    lo, hi = AMBIENT_C
    if isinstance(v, bool) or not isinstance(v, int | float) or not lo <= v <= hi:
        raise BenchError(f"ambient_c is the test's air temperature, {lo:g} to {hi:g} °C")
    return float(v)


def _from_row(row: Mapping[str, Any]) -> Bench:
    rows = tuple(_row(r) for r in json.loads(str(row["rows_json"])))
    return Bench(str(row["id"]), int(row["version"]), str(row["role"]), str(row["label"]),
                 row["motor"], row["kv"], row["prop_diameter_in"], row["prop_pitch_in"],
                 row["volts"], row["motor_mass_g"], row["source"], rows,
                 row["ambient_c"])  # fmt: skip


def benches(store: Store, platform_id: str) -> list[Bench]:
    """The platform's tables: current first, then the candidate, then the rest; within a
    role the latest change first. Two laptops can each mark a table current before they
    sync; the latest one marked counts, until a save unmarks the other."""
    rows = store.conn.execute(
        "SELECT id FROM bench_table WHERE platform_id = ? ORDER BY updated_hlc DESC, label",
        (platform_id,),
    ).fetchall()
    order = {"current": 0, "candidate": 1, "none": 2}
    found = [_from_row(store.get("bench_table", str(r[0])) or {}) for r in rows]
    return sorted(found, key=lambda b: order[b.role])


def save(store: Store, platform_id: str, values: Mapping[str, Any], bench_id: str | None = None,
         version: int | None = None) -> Bench:  # fmt: skip
    """A new table, or a change to one read at `version`. Marking a table current or
    candidate unmarks the platform's other table in that role."""
    if store.get("platform", platform_id) is None:
        raise BenchError("no such platform")
    role = str(values.get("role") or "none")
    if role not in ROLES:
        raise BenchError("role is current, candidate or none")
    label = str(values.get("label") or "").strip()[:80]
    if not label:
        raise BenchError("a table needs a label, such as the motor, KV and prop")
    record: dict[str, Any] = {
        "platform_id": platform_id, "role": role, "label": label,
        "motor": (str(values.get("motor") or "").strip()[:80] or None),
        "kv": _number(values, "kv"), "prop_diameter_in": _number(values, "prop_diameter_in"),
        "prop_pitch_in": _number(values, "prop_pitch_in"), "volts": _number(values, "volts"),
        "motor_mass_g": _number(values, "motor_mass_g"), "ambient_c": _temperature(values),
        "source": (str(values.get("source") or "").strip()[:300] or None),
        "rows_json": json.dumps([r.view() for r in check_rows(values.get("rows") or [])]),
    }  # fmt: skip
    with store.transaction():
        if role != "none":
            for other in benches(store, platform_id):
                if other.role == role and other.id != bench_id:
                    store.update("bench_table", other.id, other.version, {"role": "none"})
        if bench_id is None:
            bench_id = store.insert("bench_table", record)
        else:
            if version is None:
                raise BenchError("a change names the version it was read at")
            store.update("bench_table", bench_id, version, record)
    got = store.get("bench_table", bench_id)
    assert got is not None
    return _from_row(got)


def propulsion(model: Model, share: float) -> Model:
    """The model with its propulsion power `share` times what it gives; the avionics as
    they were."""
    return replace(model, induced=model.induced * share, flight=model.flight * share,
                   profile=model.profile * share)  # fmt: skip


def compare(current: Bench, candidate: Bench, thrust_g: float,
            density: float) -> dict[str, Any]:  # fmt: skip
    """The two tables at one motor's hover thrust: each one's power, in the air of this
    density when both give their temperature, the candidate's share of the current's, and
    what limits reading them."""
    both = current.ambient_c is not None and candidate.ambient_c is not None
    if both:
        now, then = current.power_in(thrust_g, density), candidate.power_in(thrust_g, density)
    else:
        now, then = current.power_at(thrust_g), candidate.power_at(thrust_g)
    notes: list[str] = []
    if not both and (current.ambient_c, candidate.ambient_c) != (None, None):
        notes.append("ambient_unknown")
    if (current.prop_diameter_in, current.prop_pitch_in) != (
            candidate.prop_diameter_in, candidate.prop_pitch_in):  # fmt: skip
        notes.append("props_differ")
    if current.volts and candidate.volts and abs(current.volts / candidate.volts - 1) > 0.05:
        notes.append("volts_differ")
    if now is None:
        notes.append("current_out_of_range")
    if then is None:
        notes.append("candidate_out_of_range")
    if not candidate.full_throttle:
        notes.append("candidate_short")
    return {
        "thrust_g": thrust_g, "current_w": now, "candidate_w": then,
        "share": then / now if now and then else None, "notes": notes, "same_air": both,
    }  # fmt: skip
