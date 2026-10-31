// The Propellers view (spec: Power and efficiency analysis, "Propeller and motor choice";
// mock-up Propellers): the propeller flown as each weighed hover measured it, another
// propeller (or motor) predicted against it, the propellers the setup's Priority favours,
// and what the predictions rest on. Every figure for another propeller is the engine's
// prediction (theory), worked out on this laptop from the airframe's own hover; here it
// is only laid out. Nothing here changes the aircraft.

import { readNumber } from "./bench";
import type { Frame } from "./energy";
import { flightDate, flightNumbers } from "./history";
import type { Locale, MessageKey } from "./i18n";
import { plot, type Air, type Figures, type Plot } from "./range";
import type { FlightRow } from "./report";
import { engineCall } from "./shell";

/** A propeller: its family (knowledge/rules/props.json "families"), size and blades. */
export interface Prop {
  family: string;
  diameter_in: number;
  pitch_in: number;
  blades: number;
}

/** One flight's propeller operating point in its quiet steady hover, per motor. The
 * coefficients come with the setup's diameter; the shaft power, the propeller's own C_P,
 * the drive's efficiency and the figure of merit with a motor model and the ESCs' own
 * power. The engine rounds a value it cannot give to null. */
export interface MeasuredFlight {
  flight_id: string;
  mass_kg: number | null;
  rpm: number;
  thrust_g: number;
  power_w: number;
  power_from: "escs" | "packs";
  density: number;
  seconds: number;
  rpm_spread: number;
  g_per_w: number;
  ct?: number | null;
  cp_electric?: number | null;
  tip_ms?: number | null;
  torque_nm?: number | null;
  shaft_w?: number | null;
  efficiency?: number | null;
  cp?: number | null;
  figure_of_merit?: number | null;
}

/** The motor model the aircraft flies, with where it came from. */
export interface MotorModel {
  from: string; // "tables: <labels>" or "datasheet"
  kv: number;
  kv_from: "given" | "full throttle";
  source: "table" | "datasheet";
  rms: number | null; // the fit's relative power residuals, where fitted on tables
  steps: number;
}

export interface Measured {
  flights: MeasuredFlight[];
  /** The flights that gave no hover, by the engine's reason (English engine text). */
  without: Record<string, number>;
  motor: MotorModel | null;
  diameter_in: number | null;
}

/** A propeller family's calibration: the tables and aircraft that calibrate it, and its
 * spreads (standard deviations of logarithms). */
export interface FamilyCalibration {
  id: string;
  calibrated: boolean;
  tables: number;
  aircraft: number;
  flight_noise: number | null;
  params: Record<string, number>;
  spread_thrust: number;
  spread_power: number;
  noise: number;
  rms: number | null;
  propellers: { diameter_in: number; pitch_in: number; blades: number; thrust: number; power: number; points: number }[];
}

/** One propeller change in the flights, graded against what the flights before it
 * predicted (a change of motor as well is listed, not graded). */
export interface GradedChange {
  before: string; // propeller keys, family:DxPxB
  after: string;
  flights: number;
  graded: boolean;
  why?: string;
  speed_error?: number;
  power_error?: number;
  power_largest?: number;
  inside_95?: number;
}

export interface TrackRecord {
  changes: number;
  flights: number;
  speed_error: number | null;
  power_error: number | null;
}

/** How far the local validation the advice waits on has got. */
export interface Advice {
  enabled: boolean;
  validated_error: number | null;
  gain_over_error: number;
  least_pairs: number;
  least_motors: number;
  pairs: number;
  motors: number;
}

/** `airframe.propellers`. */
export interface Propellers {
  airframe_id: string;
  missing: Record<string, string>;
  prop: Prop | null;
  priority: string | null;
  measured: Measured;
  families: FamilyCalibration[];
  track: { changes: GradedChange[]; record: TrackRecord };
  advice: Advice;
}

export type CheckName = "thrust_to_weight" | "pack_current" | "tip_mach" | "clearance" | "esc_temperature" | "notch" | "response";
export const CHECKS: CheckName[] = ["thrust_to_weight", "pack_current", "tip_mach", "clearance", "esc_temperature", "notch", "response"];

export interface Check {
  check: CheckName;
  value: number;
  limit: number;
  ok: boolean;
}

export type Objective = "hover_power" | "thrust";
export type Reason = "no_priority" | "needs_motor" | "none_pass" | "validation" | "not_calibrated" | "within_error";
export const REASONS: Reason[] = ["no_priority", "needs_motor", "none_pass", "validation", "not_calibrated", "within_error"];

/** One propeller the search tried: the objective (hover W, or thrust-to-weight), its gain
 * over the propeller flown (positive: better) and the gain's spread. */
export interface PropOption extends Prop {
  value: number;
  gain: number;
  spread: number | null;
  ok: boolean;
  failed: CheckName[];
  calibrated: boolean;
  anchored: boolean;
}

export interface Search {
  priority: string | null;
  objective: Objective | null;
  options: PropOption[];
  best: PropOption | null;
  advised: boolean;
  why: string | null;
  reason: Reason | null; // null when advised
}

export interface PropInputs {
  mass_kg: number;
  mass_from: string;
  density: number;
  air: Air;
}

/** `airframe.prop_search`: without the propeller or a weight, only what is missing. */
export interface PropSearch {
  airframe_id: string;
  missing: Record<string, string>;
  inputs?: PropInputs;
  flown_prop?: Prop;
  track?: TrackRecord;
  search?: Search;
}

export type PredictionNote = "not_anchored" | "uncalibrated" | "candidate_not_measured" | "current_not_measured" | "no_shaft_power" | "drive_efficiency_equal";
export const PREDICTION_NOTES: PredictionNote[] = [
  "not_anchored",
  "uncalibrated",
  "candidate_not_measured",
  "current_not_measured",
  "no_shaft_power",
  "drive_efficiency_equal",
];
export type ResponseNote = "mass_scaled" | "rotor_inertia_left_out" | "damping_equal";
export const RESPONSE_NOTES: ResponseNote[] = ["mass_scaled", "rotor_inertia_left_out", "damping_equal"];

/** One propeller's hover at a weight, per motor; electrical power, currents and the ESC's
 * temperature through a motor model. */
export interface HoverPoint {
  rpm: number;
  rpm_spread: number | null;
  shaft_w: number | null;
  power_spread: number | null;
  ct: number;
  cp: number | null;
  motor_hz: number;
  tip_mach: number;
  notes: PredictionNote[];
  calibrated: boolean;
  anchored: boolean;
  electric_w?: number | null;
  motor_a?: number | null;
  efficiency?: number | null;
  pack_a?: number | null;
  esc_c?: number | null;
}

export interface AtWeight {
  mass_kg: number;
  flown: HoverPoint;
  candidate: HoverPoint;
  share: number | null;
  share_from: "electric" | "shaft";
  share_spread: number | null;
}

export interface MostThrust {
  thrust_n: number;
  rpm: number;
  motor_a: number;
  electric_w: number;
  volts: number;
  tip_mach: number;
  pack_a: number;
  thrust_to_weight: number;
}

export interface ResponseTime {
  time_constant_ms: number;
  dead_time_ms: number;
  response_ms: number;
  flight_id: string;
  notes: ResponseNote[];
}

/** The flight hover both propellers are predicted from. */
export interface Anchor {
  flight_id: string;
  mass_kg: number | null;
  power_from: "escs" | "packs";
  seconds: number;
  rpm: number;
  thrust_n: number;
}

export interface Comparison {
  flown_prop: Prop;
  candidate_prop: Prop;
  other_motor: boolean;
  at: AtWeight;
  hover: AtWeight[];
  anchor: Anchor | null;
  notes: PredictionNote[];
  calibrated: boolean;
  most: { flown: MostThrust | null; candidate: MostThrust | null } | null;
  clearance_mm?: { flown: number; candidate: number | null };
  response: { flown: ResponseTime | null; candidate: ResponseTime } | null;
  checks: Check[];
}

/** `airframe.prop_candidate`. */
export interface PropCandidate {
  airframe_id: string;
  missing: Record<string, string>;
  inputs?: PropInputs;
  motor?: { from: string; kv: number; kv_from: string } | null;
  without_anchor?: Record<string, number>;
  track?: TrackRecord;
  candidate?: Comparison;
  figures?: { flown: Figures | null; candidate: Figures | null };
}

/** What the engine reads for a candidate: a propeller and, optionally, another motor
 * from its datasheet, and the propeller's own mass. */
export interface CandidateValues {
  family: string;
  diameter_in: number;
  pitch_in: number;
  blades?: number;
  motor?: { kv: number; resistance_mohm: number; no_load_a?: number };
  mass_g?: number;
}

export function propellers(airframeId: string): Promise<Propellers> {
  return engineCall<Propellers>("airframe.propellers", { airframe_id: airframeId });
}

/** The search at a weight (the setup's typical take-off when none) and air. */
export function propSearch(airframeId: string, massKg: number | null = null, air: Air = "usual"): Promise<PropSearch> {
  return engineCall<PropSearch>("airframe.prop_search", { airframe_id: airframeId, mass_kg: massKg, air });
}

export function propCandidate(airframeId: string, candidate: CandidateValues, massKg: number | null = null, air: Air = "usual"): Promise<PropCandidate> {
  return engineCall<PropCandidate>("airframe.prop_candidate", { airframe_id: airframeId, candidate, mass_kg: massKg, air });
}

// ---- numbers ----

/** A number with a fixed count of decimals in the reader's language; "–" when there is
 * none. The coefficients need more figures than formatNumber keeps. */
export function fixed(v: number | null | undefined, digits: number, locale: Locale): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  return new Intl.NumberFormat(locale === "uk" ? "uk-UA" : "en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(v);
}

/** A share as a percentage. */
export function percent(share: number | null | undefined, digits: number, locale: Locale): string {
  return share === null || share === undefined || !Number.isFinite(share) ? "–" : `${fixed(share * 100, digits, locale)}%`;
}

/** A spread worth showing: one that does not round to nothing. */
export function spreadShown(spread: number | null | undefined, digits: number): spread is number {
  return spread !== null && spread !== undefined && Number.isFinite(spread) && Math.round(spread * 100 * 10 ** digits) > 0;
}

/** A propeller's size as people write it: 22×6.6, with the blades when not two. */
export function propName(p: Pick<Prop, "diameter_in" | "pitch_in" | "blades">, locale: Locale): string {
  const n = (v: number) => new Intl.NumberFormat(locale === "uk" ? "uk-UA" : "en-GB", { maximumFractionDigits: 1 }).format(v);
  return `${n(p.diameter_in)}×${n(p.pitch_in)}${p.blades && p.blades !== 2 ? `×${p.blades}` : ""}`;
}

/** A propeller from the engine's key, family:DxPxB. */
export function keyProp(key: string): Prop | null {
  const m = /^(.+):([\d.]+)x([\d.]+)x(\d+)$/.exec(key);
  if (!m) return null;
  return { family: m[1]!, diameter_in: Number(m[2]), pitch_in: Number(m[3]), blades: Number(m[4]) };
}

export function sameProp(a: Prop, b: Prop): boolean {
  return a.family === b.family && a.diameter_in === b.diameter_in && a.pitch_in === b.pitch_in && a.blades === b.blades;
}

/** The option's gain as the column shows it: for hover power its change (less is better,
 * so a gain shows as a fall), for thrust-to-weight the rise; with its spread. The
 * propeller flown is the zero everything is measured from (its own spread, when it is
 * not anchored, says nothing about a change). */
export function gainText(o: Pick<PropOption, "gain" | "spread">, objective: Objective, locale: Locale, flown = false): string {
  const change = objective === "hover_power" ? -o.gain : o.gain;
  if (flown || (Math.round(Math.abs(change) * 1000) === 0 && !spreadShown(o.spread, 1))) return "0";
  const sign = change < 0 ? "−" : "+";
  const main = `${sign}${fixed(Math.abs(change) * 100, 1, locale)}%`;
  return spreadShown(o.spread, 1) ? `${main} ± ${fixed(o.spread * 100, 1, locale)}%` : main;
}

/** How many decimals each check's limit is shown with. */
export const LIMIT_DIGITS: Record<CheckName, number> = {
  thrust_to_weight: 1,
  pack_current: 0,
  tip_mach: 2,
  clearance: 0,
  esc_temperature: 0,
  notch: 0,
  response: 0,
};

/** A check's mark: ✓ when it holds, ▲ when the candidate fails it. */
export function checkLook(c: Pick<Check, "ok">): { glyph: string; tone: string } {
  return c.ok ? { glyph: "✓", tone: "--ok" } : { glyph: "▲", tone: "--caution" };
}

export function checkKey(name: CheckName): MessageKey {
  return `props.check.${name}`;
}

export function failedKey(name: CheckName): MessageKey {
  return `props.failed.${name}`;
}

export function reasonKey(reason: Reason): MessageKey {
  return `props.reason.${reason}`;
}

/** What the status strip says about the advice: off, with how far the validation has got
 * against what it needs; or on, with the validated error and the gain it takes. */
export function adviceText(a: Advice, locale: Locale): { key: MessageKey; vars: Record<string, string> } {
  if (a.enabled && a.validated_error !== null) {
    return {
      key: "props.status.on",
      vars: { error: fixed(a.validated_error * 100, 1, locale), threshold: fixed(a.validated_error * a.gain_over_error * 100, 1, locale) },
    };
  }
  return {
    key: "props.status.off",
    vars: { pairs: String(a.pairs), least_pairs: String(a.least_pairs), motors: String(a.motors), least_motors: String(a.least_motors) },
  };
}

/** A family's state: calibrated on tables; calibrated by aircraft flights alone (which
 * does not count: nothing is advised on it); or at its starting values. */
export function familyState(f: Pick<FamilyCalibration, "calibrated" | "aircraft">): MessageKey {
  if (f.calibrated) return "props.family.calibrated";
  return f.aircraft > 0 ? "props.family.flights" : "props.family.starting";
}

/** The setup's Priority in the reader's words, where it is one the app knows. */
export function priorityKey(priority: string | null | undefined): MessageKey {
  switch (priority) {
    case "stability":
    case "agility":
    case "endurance":
    case "efficiency":
      return `props.priority.${priority}`;
    default:
      return "props.priority.none";
  }
}

// ---- flights ----

/** A flight as the airframe's other views name it: its number in the airframe's flying
 * order, else its date, else the start of its id. */
export function flightTagger(flights: FlightRow[], locale: Locale): (id: string) => string {
  const numbers = flightNumbers(flights);
  const byId = new Map(flights.map((f) => [f.id, f]));
  return (id) => {
    const n = numbers.get(id);
    if (n !== undefined) return String(n);
    return flightDate(byId.get(id)?.utc_us ?? null, locale) ?? id.slice(0, 8);
  };
}

/** The flights that gave no hover, as "reason (count)", most first, and how many. */
export function withoutList(without: Record<string, number> | undefined): { count: number; text: string } {
  const rows = Object.entries(without ?? {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return { count: rows.reduce((s, [, n]) => s + n, 0), text: rows.map(([why, n]) => `${why} (${n})`).join(", ") };
}

// ---- the candidate ----

export type TryField = "diameter_in" | "pitch_in" | "blades" | "kv" | "resistance_mohm" | "no_load_a" | "mass_g" | "mass_kg";

/** What a person types to try a propeller, before the engine checks it. */
export interface TryDraft {
  family: string;
  diameter_in: string;
  pitch_in: string;
  blades: string;
  motor: "flown" | "other";
  kv: string;
  resistance_mohm: string;
  no_load_a: string;
  mass_g: string;
  mass_kg: string;
}

const typed = (v: number | null | undefined) => (v === null || v === undefined || !Number.isFinite(v) ? "" : String(Math.round(v * 10) / 10));

/** The form filled with a propeller (the best the search found, else the one flown) at a
 * take-off weight. */
export function fillTry(p: Prop, massKg: number | null | undefined): TryDraft {
  return {
    family: p.family,
    diameter_in: typed(p.diameter_in),
    pitch_in: typed(p.pitch_in),
    blades: String(p.blades || 2),
    motor: "flown",
    kv: "",
    resistance_mohm: "",
    no_load_a: "",
    mass_g: "",
    mass_kg: typed(massKg),
  };
}

/** The candidate the engine is asked about, or the first field that is not a number. The
 * engine checks the ranges and says why it refuses. */
export function candidateValues(d: TryDraft): { ok: true; candidate: CandidateValues; massKg: number | null } | { ok: false; field: TryField } {
  const read = (field: TryField, required: boolean): number | null | TryField => {
    const v = readNumber(d[field]);
    if (v === null) return required ? field : null;
    return Number.isFinite(v) ? v : field;
  };
  const fields: [TryField, boolean][] = [
    ["diameter_in", true],
    ["pitch_in", true],
    ["blades", false],
    ...(d.motor === "other"
      ? ([
          ["kv", true],
          ["resistance_mohm", true],
          ["no_load_a", false],
        ] as [TryField, boolean][])
      : []),
    ["mass_g", false],
    ["mass_kg", false],
  ];
  const got: Partial<Record<TryField, number | null>> = {};
  for (const [field, required] of fields) {
    const v = read(field, required);
    if (typeof v === "string") return { ok: false, field: v };
    got[field] = v;
  }
  const candidate: CandidateValues = { family: d.family, diameter_in: got.diameter_in!, pitch_in: got.pitch_in! };
  if (got.blades != null) candidate.blades = got.blades;
  if (d.motor === "other") {
    candidate.motor = { kv: got.kv!, resistance_mohm: got.resistance_mohm! };
    if (got.no_load_a != null) candidate.motor.no_load_a = got.no_load_a;
  }
  if (got.mass_g != null) candidate.mass_g = got.mass_g;
  return { ok: true, candidate, massKg: got.mass_kg ?? null };
}

export type RowId = "rpm" | "shaft" | "electric" | "pack" | "esc" | "hz" | "tip" | "most" | "packMost" | "clearance" | "response" | "figures";

/** One line of the side by side: the flown and candidate values (several on a line read
 * "a · b"), their spreads where predicted, and the check the line answers to. */
export interface CompareRow {
  id: RowId;
  flown: (number | null)[];
  candidate: (number | null)[];
  spread: { flown: number | null; candidate: number | null };
  check: Check | null;
}

const G = 9.80665;

/** The candidate against the propeller flown, line by line as the mock-up orders them; a
 * line neither has a value for (no motor model, no wheelbase, no response measured, no
 * power model) is left out. Thrust per motor in kg, times in minutes, range in km. */
export function compareRows(c: Comparison, figures?: { flown: Figures | null; candidate: Figures | null } | null): CompareRow[] {
  const a = c.at.flown, b = c.at.candidate;
  const mostA = c.most?.flown ?? null, mostB = c.most?.candidate ?? null;
  const check = (name: CheckName) => c.checks.find((x) => x.check === name) ?? null;
  const none = { flown: null, candidate: null };
  const v = (x: number | null | undefined) => (x === undefined || x === null || !Number.isFinite(x) ? null : x);
  const fa = figures?.flown ?? null, fb = figures?.candidate ?? null;
  const rows: CompareRow[] = [
    { id: "rpm", flown: [a.rpm], candidate: [b.rpm], spread: { flown: a.rpm_spread, candidate: b.rpm_spread }, check: null },
    { id: "shaft", flown: [v(a.shaft_w)], candidate: [v(b.shaft_w)], spread: { flown: a.power_spread, candidate: b.power_spread }, check: null },
    { id: "electric", flown: [v(a.electric_w)], candidate: [v(b.electric_w)], spread: { flown: a.power_spread, candidate: b.power_spread }, check: null },
    { id: "pack", flown: [v(a.pack_a)], candidate: [v(b.pack_a)], spread: none, check: null },
    { id: "esc", flown: [v(a.esc_c)], candidate: [v(b.esc_c)], spread: none, check: check("esc_temperature") },
    { id: "hz", flown: [a.motor_hz], candidate: [b.motor_hz], spread: none, check: check("notch") },
    { id: "tip", flown: [a.tip_mach, v(mostA?.tip_mach)], candidate: [b.tip_mach, v(mostB?.tip_mach)], spread: none, check: check("tip_mach") },
    {
      id: "most",
      flown: [mostA ? mostA.thrust_n / G : null, v(mostA?.thrust_to_weight)],
      candidate: [mostB ? mostB.thrust_n / G : null, v(mostB?.thrust_to_weight)],
      spread: none,
      check: check("thrust_to_weight"),
    },
    { id: "packMost", flown: [v(mostA?.pack_a)], candidate: [v(mostB?.pack_a)], spread: none, check: check("pack_current") },
    { id: "clearance", flown: [v(c.clearance_mm?.flown)], candidate: [v(c.clearance_mm?.candidate)], spread: none, check: check("clearance") },
    {
      id: "response",
      flown: [v(c.response?.flown?.response_ms)],
      candidate: [v(c.response?.candidate.response_ms)],
      spread: none,
      check: check("response"),
    },
    {
      id: "figures",
      flown: [fa ? fa.hover_s / 60 : null, fa ? fa.endurance_s / 60 : null, fa ? fa.range_m / 1000 : null],
      candidate: [fb ? fb.hover_s / 60 : null, fb ? fb.endurance_s / 60 : null, fb ? fb.range_m / 1000 : null],
      spread: none,
      check: null,
    },
  ];
  return rows.filter((r) => r.check !== null || [...r.flown, ...r.candidate].some((x) => x !== null));
}

// ---- the search ----

/** The options the table shows: the first `limit` in the engine's order (those that pass
 * every check first, best first), and the propeller flown wherever it falls. */
export function shownOptions(options: PropOption[], flown: Prop, limit: number | null): { option: PropOption; flown: boolean }[] {
  return options
    .map((option, i) => ({ option, flown: sameProp(option, flown), i }))
    .filter((r) => limit === null || r.i < limit || r.flown)
    .map(({ option, flown }) => ({ option, flown }));
}

/** What the option's notes say: measured (the flown propeller, anchored on the aircraft),
 * not a choice (fails a check), else how its prediction is calibrated and anchored. */
export function optionState(o: Pick<PropOption, "ok" | "calibrated" | "anchored">, flown: boolean): MessageKey[] {
  if (flown) return [o.anchored ? "props.state.measured" : "props.state.flownTheory"];
  if (!o.ok) return ["props.state.notChoice"];
  return [o.calibrated ? "props.state.calibrated" : "props.state.uncalibrated", o.anchored ? "props.state.anchored" : "props.state.notAnchored"];
}

/** What the search tried: pitch over diameter from the lowest to the highest, and the
 * diameters. */
export function searchSpan(options: Pick<Prop, "diameter_in" | "pitch_in">[]): { lo: number; hi: number; diameters: number[] } | null {
  if (!options.length) return null;
  const ratios = options.map((o) => o.pitch_in / o.diameter_in);
  const round = (x: number) => Math.round(x * 100) / 100;
  return {
    lo: round(Math.min(...ratios)),
    hi: round(Math.max(...ratios)),
    diameters: [...new Set(options.map((o) => o.diameter_in))].sort((a, b) => a - b),
  };
}

export interface SearchLine {
  diameter: number;
  flown: boolean; // the flown propeller's diameter
  rank: number; // 0 for the smallest diameter, 1 for the largest
  solid: string; // SVG path between options that pass every check
  dashed: string; // the stretches touching an option that fails one
  end: { x: number; y: number }; // where the line ends, for its label
}

export interface SearchChart extends Plot {
  lines: SearchLine[];
  flown: { x: number; y: number } | null;
  best: { x: number; y: number } | null;
}

/** The search as one line per diameter: the objective against pitch over diameter, the
 * stretches touching an option that fails a check dashed, with where the propeller flown
 * and the best that passes every check sit. Null with fewer than two options. */
export function searchChart(options: PropOption[], flown: Prop, best: PropOption | null, f: Frame): SearchChart | null {
  const usable = options.filter((o) => Number.isFinite(o.value) && o.diameter_in > 0);
  if (usable.length < 2) return null;
  const ratio = (o: Prop) => o.pitch_in / o.diameter_in;
  // a tenth of the values' range either side, so the marks and their labels clear the axes
  const values = usable.map((o) => o.value);
  const lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.1;
  const p = plot(usable.map(ratio), [...values, lo - pad, hi + pad], f, null, null);
  const diameters = [...new Set(usable.map((o) => o.diameter_in))].sort((a, b) => a - b);
  const lines = diameters.map((d, k) => {
    const pts = usable
      .filter((o) => o.diameter_in === d)
      .sort((a, b) => ratio(a) - ratio(b))
      .map((o) => ({ x: p.px(ratio(o)), y: p.py(o.value), ok: o.ok }));
    const solid: string[] = [];
    const dashed: string[] = [];
    pts.forEach((q, i) => {
      const at = `${q.x.toFixed(1)} ${q.y.toFixed(1)}`;
      if (i === 0) {
        if (pts.length === 1) (q.ok ? solid : dashed).push(`M${at} L${at}`);
        return;
      }
      const prev = pts[i - 1]!;
      const from = `${prev.x.toFixed(1)} ${prev.y.toFixed(1)}`;
      (prev.ok && q.ok ? solid : dashed).push(`M${from} L${at}`);
    });
    const last = pts[pts.length - 1]!;
    return {
      diameter: d,
      flown: d === flown.diameter_in,
      rank: diameters.length > 1 ? k / (diameters.length - 1) : 0,
      solid: solid.join(" "),
      dashed: dashed.join(" "),
      end: { x: last.x, y: last.y },
    };
  });
  const where = (target: Prop | null) => {
    const o = target ? usable.find((x) => sameProp(x, target)) : undefined;
    return o ? { x: p.px(ratio(o)), y: p.py(o.value) } : null;
  };
  return { ...p, lines, flown: where(flown), best: where(best) };
}
