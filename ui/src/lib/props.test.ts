import { describe, expect, it } from "vitest";
import { CATALOGS } from "./i18n";
import {
  adviceText,
  candidateValues,
  CHECKS,
  checkKey,
  checkLook,
  compareRows,
  failedKey,
  familyState,
  fillTry,
  fixed,
  flightTagger,
  gainText,
  keyProp,
  LIMIT_DIGITS,
  optionState,
  percent,
  PREDICTION_NOTES,
  priorityKey,
  propName,
  REASONS,
  reasonKey,
  RESPONSE_NOTES,
  sameProp,
  searchChart,
  searchSpan,
  shownOptions,
  spreadShown,
  withoutList,
  type Comparison,
  type HoverPoint,
  type Prop,
  type PropOption,
} from "./props";
import type { FlightRow } from "./report";

// Made-up airframes, in the engine's shapes (engine/src/flight_insights/performance/
// report.py prop_overview, prop_search, prop_candidate).
const FLOWN: Prop = { family: "tmotor_p", diameter_in: 22, pitch_in: 6.6, blades: 2 };

const option = (d: number, p: number, value: number, gain: number, spread: number | null, failed: PropOption["failed"] = []): PropOption => ({
  family: "tmotor_p",
  diameter_in: d,
  pitch_in: p,
  blades: 2,
  value,
  gain,
  spread,
  ok: failed.length === 0,
  failed,
  calibrated: true,
  anchored: true,
});

// the search, best first, those failing a check last (as propchoice.search orders them)
const OPTIONS: PropOption[] = [
  option(24, 7.2, 752.4, 0.0641, 0.06),
  option(24, 8.6, 757.6, 0.0575, 0.0615),
  option(24, 10.1, 767.5, 0.0453, 0.0648),
  option(22, 6.6, 803.9, 0, 0),
  option(22, 5.3, 806.6, -0.0034, 0.063),
  option(22, 7.9, 807.5, -0.0045, 0.0615),
  option(22, 9.2, 815.2, -0.0141, 0.0645),
  option(20, 6.0, 868.4, -0.0802, 0.06),
  option(20, 7.2, 870.6, -0.0829, 0.0615),
  option(20, 8.4, 877.0, -0.091, 0.0645),
  option(24, 5.8, 753.1, 0.0632, 0.063, ["tip_mach"]),
  option(20, 4.8, 873.1, -0.0861, 0.063, ["thrust_to_weight"]),
];

const point = (over: Partial<HoverPoint> = {}): HoverPoint => ({
  rpm: 2726.8,
  rpm_spread: 0.119,
  shaft_w: 472.8,
  power_spread: 0.1305,
  ct: 0.0617,
  cp: 0.017,
  motor_hz: 45.4,
  tip_mach: 0.3197,
  notes: ["not_anchored", "uncalibrated"],
  calibrated: false,
  anchored: false,
  ...over,
});

// without a motor model: no electrical power, currents, ESC temperature or most thrust
const PLAIN: Comparison = {
  flown_prop: { family: "carbon", diameter_in: 30, pitch_in: 10, blades: 2 },
  candidate_prop: { family: "carbon", diameter_in: 32, pitch_in: 11, blades: 2 },
  other_motor: false,
  at: { mass_kg: 40.5, flown: point(), candidate: point({ rpm: 2364.8, shaft_w: 441.9, tip_mach: 0.2957 }), share: 0.9346, share_from: "shaft", share_spread: 0.13 },
  hover: [],
  anchor: null,
  notes: ["candidate_not_measured", "drive_efficiency_equal", "not_anchored", "uncalibrated"],
  calibrated: false,
  most: null,
  clearance_mm: { flown: 174.9, candidate: 124.1 },
  response: null,
  checks: [
    { check: "tip_mach", value: 0.2957, limit: 0.7, ok: true },
    { check: "clearance", value: 124.1, limit: 10, ok: true },
  ],
};

// with a motor model, an anchor and a measured response
const most = (thrust_n: number, ratio: number, pack_a: number, tip: number) => ({
  thrust_n,
  rpm: 7500,
  motor_a: 80,
  electric_w: 3000,
  volts: 41.16,
  tip_mach: tip,
  pack_a,
  thrust_to_weight: ratio,
});
const DRIVEN: Comparison = {
  ...PLAIN,
  flown_prop: FLOWN,
  candidate_prop: { ...FLOWN, diameter_in: 24, pitch_in: 7.2 },
  at: {
    mass_kg: 20,
    flown: point({ rpm: 5103.3, rpm_spread: 0, power_spread: 0, electric_w: 803.9, pack_a: 67, esc_c: 30.6, notes: [], anchored: true, calibrated: true }),
    candidate: point({ rpm: 4300.9, rpm_spread: 0.03, power_spread: 0.06, electric_w: 752.4, pack_a: 62.7, esc_c: 29.9 }),
    share: 0.9359,
    share_from: "electric",
    share_spread: 0.06,
  },
  anchor: { flight_id: "f1", mass_kg: 20, power_from: "escs", seconds: 60, rpm: 5103, thrust_n: 49 },
  notes: ["candidate_not_measured", "current_not_measured"],
  most: { flown: most(118.8446, 2.4238, 284.9, 0.6644), candidate: most(153.9117, 3.1389, 400.2, 0.6901) },
  response: {
    flown: { time_constant_ms: 30, dead_time_ms: 10, response_ms: 40, flight_id: "f1", notes: [] },
    candidate: { time_constant_ms: 41.3, dead_time_ms: 10, response_ms: 51.3, flight_id: "f1", notes: ["mass_scaled"] },
  },
  checks: [
    { check: "thrust_to_weight", value: 3.1389, limit: 1.6, ok: true },
    { check: "pack_current", value: 400.2, limit: 300, ok: false },
    { check: "tip_mach", value: 0.6901, limit: 0.7, ok: true },
    { check: "clearance", value: 380.3, limit: 10, ok: true },
    { check: "esc_temperature", value: 29.9, limit: 100, ok: true },
    { check: "notch", value: 71.7, limit: 20, ok: true },
    { check: "response", value: 51.3, limit: 60, ok: true },
  ],
};

describe("numbers", () => {
  it("keeps the decimals asked for, in the reader's language", () => {
    expect(fixed(0.0612, 4, "en")).toBe("0.0612");
    expect(fixed(2880.4, 0, "en")).toBe("2,880");
    expect(fixed(0.0612, 4, "uk")).toBe("0,0612");
    expect(fixed(null, 1, "en")).toBe("–");
    expect(fixed(NaN, 1, "en")).toBe("–");
    expect(percent(0.86, 0, "en")).toBe("86%");
    expect(percent(undefined, 0, "en")).toBe("–");
  });

  it("shows a spread only when it does not round to nothing", () => {
    expect(spreadShown(0.06, 0)).toBe(true);
    expect(spreadShown(0.004, 0)).toBe(false);
    expect(spreadShown(0.004, 1)).toBe(true);
    expect(spreadShown(0, 1)).toBe(false);
    expect(spreadShown(null, 1)).toBe(false);
  });
});

describe("propellers by name", () => {
  it("writes the size as people do, with the blades when not two", () => {
    expect(propName(FLOWN, "en")).toBe("22×6.6");
    expect(propName({ diameter_in: 30, pitch_in: 10, blades: 3 }, "en")).toBe("30×10×3");
    expect(propName(FLOWN, "uk")).toBe("22×6,6");
  });

  it("reads the engine's propeller keys", () => {
    expect(keyProp("tmotor_p:22x6.6x2")).toEqual(FLOWN);
    expect(keyProp("not a key")).toBeNull();
    expect(sameProp(keyProp("tmotor_p:22x6.6x2")!, FLOWN)).toBe(true);
    expect(sameProp({ ...FLOWN, blades: 3 }, FLOWN)).toBe(false);
  });
});

describe("the search", () => {
  it("shows hover power's gain as its fall, and thrust's as its rise, with the spread", () => {
    expect(gainText(OPTIONS[0]!, "hover_power", "en")).toBe("−6.4% ± 6.0%");
    expect(gainText(option(31, 8.1, 475.8, -0.0063, 0.1447), "hover_power", "en")).toBe("+0.6% ± 14.5%");
    expect(gainText({ gain: 0.05, spread: 0.02 }, "thrust", "en")).toBe("+5.0% ± 2.0%");
    expect(gainText({ gain: 0.05, spread: null }, "thrust", "en")).toBe("+5.0%");
    expect(gainText(OPTIONS[3]!, "hover_power", "en")).toBe("0");
    // the propeller flown, not anchored, has a spread of its own: against itself it is 0
    expect(gainText({ gain: 0, spread: 0.1305 }, "hover_power", "en", true)).toBe("0");
    expect(gainText({ gain: 0, spread: 0.1305 }, "hover_power", "en")).toBe("+0.0% ± 13.1%");
    expect(gainText({ gain: 0.0834, spread: 0.1258 }, "hover_power", "uk")).toBe("−8,3% ± 12,6%");
  });

  it("shows the first options and the propeller flown wherever it falls", () => {
    const few = shownOptions(OPTIONS, FLOWN, 2);
    expect(few.map((r) => propName(r.option, "en"))).toEqual(["24×7.2", "24×8.6", "22×6.6"]);
    expect(few.map((r) => r.flown)).toEqual([false, false, true]);
    expect(shownOptions(OPTIONS, FLOWN, null)).toHaveLength(OPTIONS.length);
  });

  it("says what each option's prediction rests on", () => {
    expect(optionState(OPTIONS[3]!, true)).toEqual(["props.state.measured"]);
    expect(optionState({ ...OPTIONS[3]!, anchored: false }, true)).toEqual(["props.state.flownTheory"]);
    expect(optionState(OPTIONS[10]!, false)).toEqual(["props.state.notChoice"]);
    expect(optionState(OPTIONS[0]!, false)).toEqual(["props.state.calibrated", "props.state.anchored"]);
    expect(optionState({ ...OPTIONS[0]!, calibrated: false, anchored: false }, false)).toEqual(["props.state.uncalibrated", "props.state.notAnchored"]);
  });

  it("finds the pitches and diameters tried", () => {
    const tried = [28, 30, 32].flatMap((d) => [0.24, 0.42].map((r) => ({ diameter_in: d, pitch_in: Math.round(r * d * 10) / 10 })));
    expect(searchSpan(tried)).toEqual({ lo: 0.24, hi: 0.42, diameters: [28, 30, 32] });
    expect(searchSpan([])).toBeNull();
  });

  it("draws one line per diameter, dashed where an option fails a check", () => {
    const frame = { width: 1000, height: 280, left: 64, right: 44, top: 18, bottom: 46 };
    const c = searchChart(OPTIONS, FLOWN, OPTIONS[0]!, frame)!;
    expect(c.lines.map((l) => l.diameter)).toEqual([20, 22, 24]);
    expect(c.lines.map((l) => l.flown)).toEqual([false, true, false]);
    expect(c.lines.map((l) => l.rank)).toEqual([0, 0.5, 1]);
    // 20×4.8 and 24×5.8 fail a check: the stretch to the next pitch is dashed
    expect(c.lines[0]!.dashed.split("M")).toHaveLength(2);
    expect(c.lines[1]!.dashed).toBe("");
    expect(c.lines[2]!.dashed.split("M")).toHaveLength(2);
    expect(c.lines[1]!.solid.split("M")).toHaveLength(4); // four pitches, three stretches
    // the flown and the best sit on their lines; less power is lower
    expect(c.flown).not.toBeNull();
    expect(c.best!.y).toBeGreaterThan(frame.top);
    expect(c.best!.y).toBeLessThan(frame.height - frame.bottom - 8); // the axis is padded
    expect(c.best!.y).toBeGreaterThan(c.flown!.y);
    expect(c.y.lo).toBeLessThanOrEqual(752.4);
    expect(c.y.hi).toBeGreaterThanOrEqual(877);
    expect(c.x.lo).toBeLessThanOrEqual(4.8 / 20);
    expect(searchChart(OPTIONS.slice(0, 1), FLOWN, null, frame)).toBeNull();
  });

  it("has words for every reason, check and note the engine gives", () => {
    for (const r of REASONS) expect(CATALOGS.en[reasonKey(r)]).toBeTruthy();
    for (const c of CHECKS) {
      expect(CATALOGS.uk[checkKey(c)]).toContain("{limit}");
      expect(CATALOGS.en[failedKey(c)]).toBeTruthy();
      expect(LIMIT_DIGITS[c]).toBeGreaterThanOrEqual(0);
    }
    for (const n of PREDICTION_NOTES) expect(CATALOGS.uk[`props.note.${n}`]).toBeTruthy();
    for (const n of RESPONSE_NOTES) expect(CATALOGS.uk[`props.response.${n}`]).toBeTruthy();
  });
});

describe("the advice and the setup", () => {
  const ADVICE = { enabled: false, validated_error: null, gain_over_error: 2, least_pairs: 10, least_motors: 3, pairs: 2, motors: 1 };

  it("says how far the validation has got, or the error it found", () => {
    expect(adviceText(ADVICE, "en")).toEqual({
      key: "props.status.off",
      vars: { pairs: "2", least_pairs: "10", motors: "1", least_motors: "3" },
    });
    // switched on without a validated error is still off
    expect(adviceText({ ...ADVICE, enabled: true }, "en").key).toBe("props.status.off");
    expect(adviceText({ ...ADVICE, enabled: true, validated_error: 0.02 }, "en")).toEqual({
      key: "props.status.on",
      vars: { error: "2.0", threshold: "4.0" },
    });
  });

  it("names a family's state and the setup's Priority", () => {
    expect(familyState({ calibrated: true, aircraft: 0 })).toBe("props.family.calibrated");
    expect(familyState({ calibrated: false, aircraft: 2 })).toBe("props.family.flights");
    expect(familyState({ calibrated: false, aircraft: 0 })).toBe("props.family.starting");
    expect(priorityKey("endurance")).toBe("props.priority.endurance");
    expect(priorityKey("agility")).toBe("props.priority.agility");
    expect(priorityKey(null)).toBe("props.priority.none");
    expect(priorityKey("speed")).toBe("props.priority.none");
  });
});

describe("flights", () => {
  const row = (id: string, utc_us: number | null): FlightRow => ({
    id,
    airframe_id: "a",
    airframe: "HL-07",
    utc_us,
    duration_s: null,
    firmware: null,
    payload: null,
    modes: [],
    analysis: null,
  });

  it("names a flight by its number in the flying order, else by the start of its id", () => {
    const tag = flightTagger([row("f3", 3), row("f2", 2), row("f1", 1)], "en");
    expect(tag("f1")).toBe("1");
    expect(tag("f3")).toBe("3");
    expect(tag("0e2f29b59767b36f")).toBe("0e2f29b5");
  });

  it("lists why flights gave no hover, most first", () => {
    expect(withoutList({ "less than 10 s of quiet hover": 1, "no ESC speed telemetry": 2 })).toEqual({
      count: 3,
      text: "no ESC speed telemetry (2), less than 10 s of quiet hover (1)",
    });
    expect(withoutList(undefined)).toEqual({ count: 0, text: "" });
  });
});

describe("trying a propeller", () => {
  const filled = fillTry({ family: "carbon", diameter_in: 32, pitch_in: 13.4, blades: 2 }, 40.5);

  it("fills the form with a propeller and a weight", () => {
    expect(filled).toMatchObject({ family: "carbon", diameter_in: "32", pitch_in: "13.4", blades: "2", motor: "flown", mass_kg: "40.5", mass_g: "" });
    expect(fillTry(FLOWN, null).mass_kg).toBe("");
  });

  it("reads what a person types for the engine", () => {
    expect(candidateValues(filled)).toEqual({
      ok: true,
      candidate: { family: "carbon", diameter_in: 32, pitch_in: 13.4, blades: 2 },
      massKg: 40.5,
    });
    const other = { ...filled, pitch_in: "13,4", blades: "", motor: "other" as const, kv: "210", resistance_mohm: "35", mass_g: "95", mass_kg: "" };
    expect(candidateValues(other)).toEqual({
      ok: true,
      candidate: { family: "carbon", diameter_in: 32, pitch_in: 13.4, motor: { kv: 210, resistance_mohm: 35 }, mass_g: 95 },
      massKg: null,
    });
    expect(candidateValues({ ...other, no_load_a: "1.2" })).toMatchObject({ candidate: { motor: { kv: 210, resistance_mohm: 35, no_load_a: 1.2 } } });
  });

  it("names the first field that is not a number, and leaves ranges to the engine", () => {
    expect(candidateValues({ ...filled, diameter_in: "" })).toEqual({ ok: false, field: "diameter_in" });
    expect(candidateValues({ ...filled, pitch_in: "abc" })).toEqual({ ok: false, field: "pitch_in" });
    expect(candidateValues({ ...filled, motor: "other" })).toEqual({ ok: false, field: "kv" });
    expect(candidateValues({ ...filled, mass_kg: "heavy" })).toEqual({ ok: false, field: "mass_kg" });
    expect(candidateValues({ ...filled, kv: "abc" }).ok).toBe(true); // the flown motor: Kv is not read
    expect(candidateValues({ ...filled, blades: "-3" })).toMatchObject({ ok: true, candidate: { blades: -3 } });
  });

  it("lays out the side by side, leaving out what neither propeller has", () => {
    const plain = compareRows(PLAIN, {
      flown: { mass_kg: 40.5, hover_s: 752.5, endurance_s: 793.7, endurance_ms: 9.35, range_m: 9335.4, range_ms: 12, radius_m: {}, measured: false, efficiency: { figure_of_merit: 0.88, grams_per_watt: 9.5, wh_per_km: 98.6, wh_per_kg_km: 2.4, cost_of_transport: 0.9 } },
      candidate: null,
    });
    expect(plain.map((r) => r.id)).toEqual(["rpm", "shaft", "hz", "tip", "clearance", "figures"]);
    expect(plain.find((r) => r.id === "tip")).toMatchObject({ flown: [0.3197, null], check: { check: "tip_mach", ok: true } });
    expect(plain.find((r) => r.id === "rpm")?.spread).toEqual({ flown: 0.119, candidate: 0.119 });
    expect(plain.find((r) => r.id === "figures")?.flown.map((v) => Math.round(v! * 10) / 10)).toEqual([12.5, 13.2, 9.3]);
    expect(plain.find((r) => r.id === "figures")?.candidate).toEqual([null, null, null]);

    const driven = compareRows(DRIVEN);
    expect(driven.map((r) => r.id)).toEqual(["rpm", "shaft", "electric", "pack", "esc", "hz", "tip", "most", "packMost", "clearance", "response"]);
    const mostRow = driven.find((r) => r.id === "most")!;
    expect(mostRow.flown.map((v) => Math.round(v! * 100) / 100)).toEqual([12.12, 2.42]);
    expect(mostRow.check?.check).toBe("thrust_to_weight");
    expect(driven.find((r) => r.id === "packMost")?.check).toMatchObject({ check: "pack_current", ok: false });
    expect(driven.find((r) => r.id === "response")).toMatchObject({ flown: [40], candidate: [51.3], check: { check: "response" } });
    expect(driven.find((r) => r.id === "esc")?.check?.check).toBe("esc_temperature");
    expect(driven.find((r) => r.id === "hz")?.check?.check).toBe("notch");
  });

  it("marks a check that holds and one that fails", () => {
    expect(checkLook({ ok: true })).toEqual({ glyph: "✓", tone: "--ok" });
    expect(checkLook({ ok: false })).toEqual({ glyph: "▲", tone: "--caution" });
  });
});
