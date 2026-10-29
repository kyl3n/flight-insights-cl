// Makers' bench tables and two builds side by side (spec: Power and efficiency analysis,
// "Range, endurance and speed"; mock-up Performance, "Build against build"). A person
// enters each table for the platform; the engine compares the current parts with a
// candidate set on the airframe's own model. Nothing here is fetched from a maker.
import type { Figures } from "./range";
import { engineCall } from "./shell";

export type Role = "current" | "candidate" | "none";
export const ROLES: Role[] = ["current", "candidate", "none"];

export interface Step {
  throttle_pct: number;
  thrust_g: number;
  power_w: number;
}

export interface BenchTable {
  id: string;
  version: number;
  role: Role;
  label: string;
  motor: string | null;
  kv: number | null;
  prop_diameter_in: number | null;
  prop_pitch_in: number | null;
  volts: number | null;
  motor_mass_g: number | null;
  source: string | null;
  ambient_c: number | null;
  rows: Step[];
  max_thrust_g: number;
  full_throttle: boolean;
}

export type Note = "props_differ" | "volts_differ" | "current_out_of_range" | "candidate_out_of_range" | "candidate_short" | "ambient_unknown";

export interface Builds {
  platform_id: string;
  tables: BenchTable[];
  motors: number;
  missing?: "current" | "candidate";
  flown_vs_bench?: { thrust_g: number; flown_w: number; bench_w: number | null; share: number | null; same_air: boolean };
  compare?: {
    thrust_g: number;
    current_w: number | null;
    candidate_w: number | null;
    share: number | null;
    notes: Note[];
    same_air: boolean;
    mass_kg: number;
    delta_kg: number;
  };
  headroom?: { current: number; candidate: number };
  figures?: { current: Figures; candidate: Figures | null };
  power?: { airspeed_ms: number[]; current_w: number[]; candidate_w: number[] };
  against_weight?: { mass_kg: number; range_m: number | null; endurance_s: number | null }[];
}

/** What a person types for a table, before the engine checks it. */
export interface Draft {
  role: Role;
  label: string;
  motor: string;
  kv: string;
  prop_diameter_in: string;
  prop_pitch_in: string;
  volts: string;
  motor_mass_g: string;
  source: string;
  ambient_c: string;
  steps: string;
}

export function emptyDraft(role: Role): Draft {
  return { role, label: "", motor: "", kv: "", prop_diameter_in: "", prop_pitch_in: "", volts: "", motor_mass_g: "", source: "", ambient_c: "", steps: "" };
}

const text = (v: number | null) => (v === null ? "" : String(v));

export function draftOf(b: BenchTable): Draft {
  return {
    role: b.role,
    label: b.label,
    motor: b.motor ?? "",
    kv: text(b.kv),
    prop_diameter_in: text(b.prop_diameter_in),
    prop_pitch_in: text(b.prop_pitch_in),
    volts: text(b.volts),
    motor_mass_g: text(b.motor_mass_g),
    source: b.source ?? "",
    ambient_c: text(b.ambient_c),
    steps: b.rows.map((r) => `${r.throttle_pct}\t${r.thrust_g}\t${r.power_w}`).join("\n"),
  };
}

/** A number as people type it: a decimal comma reads as a point. Empty is null; anything
 * else that is not a number is NaN, for the engine to refuse with its reason. */
export function readNumber(s: string): number | null {
  const v = s.trim().replace(",", ".");
  return v === "" ? null : Number(v);
}

/** Where a maker's own heading puts each column, counted from the throttle column, so a
 * whole pasted table reads whatever else it holds: T-Motor's runs Item No. | Prop |
 * Throttle | Voltage | Thrust | Torque | Current | RPM | Power | Efficiency | Temperature,
 * with the item and prop only on the first row. Power comes from its own column, else
 * from voltage times current. */
interface Heading {
  thrust: number;
  thrustScale: number; // 1000 when the heading gives kg
  power: number | null;
  volts: number | null;
  amps: number | null;
}

function heading(cells: string[]): Heading | null {
  const at = (re: RegExp, not?: RegExp) => cells.findIndex((c) => re.test(c) && !(not && not.test(c)));
  const throttle = at(/throttle/i);
  const thrust = at(/thrust/i);
  if (throttle < 0 || thrust < 0) return null;
  const rel = (i: number) => (i < 0 ? null : i - throttle);
  return {
    thrust: thrust - throttle,
    thrustScale: /\bkg/i.test(cells[thrust]!) ? 1000 : 1,
    power: rel(at(/power/i, /max|efficien/i)),
    volts: rel(at(/volt/i)),
    amps: rel(at(/current|amp/i, /idle|peak/i)),
  };
}

const PERCENT = /^\d+(?:[.,]\d+)?\s*%$/;
const number = (c: string | undefined) => (c === undefined ? null : readNumber(c.replace(/\s+/g, "").replace(/[%a-zA-Z℃°]+$/, "")));

/** The steps pasted from a maker's sheet: throttle %, thrust in g and power in W.
 *
 * A whole table pasted with its heading (cells split by tabs or semicolons) is read by
 * the heading's column names, from the cell that holds the throttle percentage on each
 * row. Without a heading, each line must hold exactly three numbers in that order: cells
 * split by tabs or semicolons when the line has them (a space inside a cell is a
 * thousands gap, a comma a decimal comma), else by spaces, else by commas. A % or unit
 * after a number is dropped. A line without numbers (a title) is skipped; a line with
 * numbers that cannot be read either way is refused and counted, never guessed at, since
 * a sheet's other columns (volts, amps, rpm) come in no fixed order. A page that lists
 * several motors or props under one heading gives one table per row naming its item or
 * prop; `tables` counts them, and only one may be saved at a time. The bench's air
 * temperature is read where the maker writes it ("Ambient Temperature: 22℃"). */
export function parseSteps(s: string): { steps: Step[]; refused: number; tables: number; ambient_c: number | null } {
  const steps: Step[] = [];
  let refused = 0;
  let tables = 0; // rows that name their item or prop before the throttle start a table
  let head: Heading | null = null;
  for (const raw of s.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const tabbed = /[\t;]/.test(line);
    if (tabbed) {
      const found = heading(line.split(/[\t;]/));
      if (found) {
        head = found;
        continue;
      }
    }
    if (head && !tabbed) continue; // a note under the table, such as how it was measured
    if (head) {
      const cells = line.split(/[\t;]/).map((c) => c.trim());
      const t = cells.findIndex((c) => PERCENT.test(c));
      if (t > 0 && cells.slice(0, t).some((c) => c !== "")) tables += 1;
      const throttle = t < 0 ? null : number(cells[t]);
      const thrust = t < 0 ? null : number(cells[t + head.thrust]);
      const cell = (k: number | null) => (k === null || t < 0 ? null : number(cells[t + k]));
      const w = cell(head.power);
      const v = cell(head.volts), a = cell(head.amps);
      const power = w ?? (v !== null && a !== null ? v * a : null);
      if (throttle !== null && thrust !== null && power !== null && [throttle, thrust, power].every(Number.isFinite)) {
        steps.push({ throttle_pct: throttle, thrust_g: thrust * head.thrustScale, power_w: power });
      } else if (cells.some((c) => number(c) !== null && Number.isFinite(number(c)))) refused += 1;
      continue;
    }
    const cells = tabbed
      ? line.split(/[\t;]+/).map((c) => c.replace(/\s+/g, ""))
      : /\s/.test(line)
        ? line.split(/\s+/)
        : line.split(",");
    const nums = cells.map((c) => number(c)).filter((v): v is number => v !== null && Number.isFinite(v));
    if (nums.length === 3) steps.push({ throttle_pct: nums[0]!, thrust_g: nums[1]!, power_w: nums[2]! });
    else if (nums.length) refused += 1;
  }
  // the bench's air temperature, where the maker writes it beside the table
  const ambient = /ambient\s*temperature\s*[:：]?\s*(-?\d+(?:[.,]\d+)?)\s*(?:℃|°\s*C)/i.exec(s);
  return { steps, refused, tables: Math.max(tables, steps.length ? 1 : 0), ambient_c: ambient ? readNumber(ambient[1]!) : null };
}

export function valuesOf(d: Draft): Record<string, unknown> {
  return {
    role: d.role,
    label: d.label,
    motor: d.motor,
    kv: readNumber(d.kv),
    prop_diameter_in: readNumber(d.prop_diameter_in),
    prop_pitch_in: readNumber(d.prop_pitch_in),
    volts: readNumber(d.volts),
    motor_mass_g: readNumber(d.motor_mass_g),
    source: d.source,
    ambient_c: readNumber(d.ambient_c),
    rows: parseSteps(d.steps).steps,
  };
}

export function benchList(platformId: string): Promise<BenchTable[]> {
  return engineCall<BenchTable[]>("bench.list", { platform_id: platformId });
}

export function benchSave(platformId: string, d: Draft, table: BenchTable | null): Promise<BenchTable> {
  const params: Record<string, unknown> = { platform_id: platformId, values: valuesOf(d) };
  if (table) {
    params.id = table.id;
    params.version = table.version;
  }
  return engineCall<BenchTable>("bench.save", params);
}

/** The share of the most a table was measured to give that hover needs, per motor. */
export function hoverShare(thrustG: number, b: BenchTable): number {
  return thrustG / b.max_thrust_g;
}
