// An airframe's range, flight time and speeds, as the engine's airframe.performance returns
// them, and the helpers behind the Range and speed screen (spec: Power and efficiency
// analysis, "Range, endurance and speed"; mock-up Performance). The figures are the
// engine's: worked out on this laptop from the airframe's own flights. Here they are
// only laid out.

import { axis, type Axis, type Frame } from "./energy";
import { engineCall } from "./shell";

export type Air = "usual" | "standard";
export type Trip = "out_and_back" | "drop" | "site";

export interface Spread {
  low: number;
  median: number;
  high: number;
}

export interface Efficiency {
  figure_of_merit: number;
  grams_per_watt: number;
  wh_per_km: number;
  wh_per_kg_km: number;
  cost_of_transport: number;
}

export interface Figures {
  mass_kg: number;
  hover_s: number;
  endurance_s: number;
  endurance_ms: number;
  range_m: number;
  range_ms: number;
  radius_m: Partial<Record<Trip, number>>;
  measured: boolean;
  efficiency: Efficiency;
}

export interface Inputs {
  mass_kg: number | null;
  mass_from: string;
  density: number;
  air: Air;
  empty_kg: number | null;
  max_kg: number | null;
  flown_kg: number[] | null;
  top_ms: number | null;
  reserve_share: number;
  trip: Trip;
  wind_ms: number;
}

export interface Speeds {
  endurance: number;
  endurance_band: [number, number];
  range: number;
  range_band: [number, number];
  long_range: number;
  endurance_w: number;
  range_wh_per_km: number;
}

export interface TopSpeed {
  airspeed: number;
  by: string; // "lean", "thrust" or "current"
  extrapolated: boolean;
}

export interface Performance {
  airframe_id: string;
  missing: Record<string, string>;
  sources: Record<string, string>;
  level: "A" | "B" | "C" | null;
  why: string | null;
  inputs?: Inputs;
  speeds?: Speeds;
  figures?: Figures | null;
  spread?: Partial<Record<"hover_s" | "endurance_s" | "range_m" | "radius_m", Spread>>;
  against_weight?: Figures[];
  radius?: { trip: Trip; wind_ms: number; worst_m: number; by_direction: [number, number][] };
  hover?: { mass_kg: number; measured: boolean; hover_s: Spread };
  top_speed?: TopSpeed;
  ends?: Record<"hover" | "endurance" | "range", End>;
  failsafe?: Failsafe;
}

/** What ends a steady flight: the reserve, or something the pack or parameters set sooner. */
export type End = "reserve" | "capacity_failsafe" | "voltage_failsafe" | "cutoff" | "empty" | "power" | "current" | "time";

export interface Failsafe {
  low_volt: number | null;
  timer_s: number;
  resting: boolean;
  capacity_mah: number | null;
  low_mah: number | null;
  current_limit_a: number | null;
}

/** The failsafes that end any figure before the reserve does. */
export function earlyEnds(ends: Performance["ends"]): End[] {
  const out = new Set<End>();
  for (const e of Object.values(ends ?? {})) if (e === "capacity_failsafe" || e === "voltage_failsafe") out.add(e);
  return [...out];
}

export interface Asked {
  mass_kg: number | null;
  air: Air;
  trip: Trip;
  wind_ms: number;
}

export function airframePerformance(airframeId: string, asked: Asked): Promise<Performance> {
  const params: Record<string, unknown> = { airframe_id: airframeId, air: asked.air, trip: asked.trip, wind_ms: asked.wind_ms };
  if (asked.mass_kg !== null) params.mass_kg = asked.mass_kg;
  return engineCall<Performance>("airframe.performance", params);
}

export const TRIPS: Trip[] = ["out_and_back", "drop", "site"];
export const WINDS = [0, 5, 10];

export interface WeightChoice {
  kg: number | null; // null: the engine's own pick (the setup's typical take-off)
  role: "typical" | "empty" | "flown" | "max";
}

/** The take-off weights to pick from: the engine's own pick first, then the empty weight,
 * each weight flown and the most it may carry, each weight once. */
export function weights(i: Inputs | undefined): WeightChoice[] {
  const out: WeightChoice[] = [{ kg: null, role: "typical" }];
  const seen = new Set<number>(i?.mass_kg !== null && i?.mass_kg !== undefined ? [round1(i.mass_kg)] : []);
  const add = (kg: number | null | undefined, role: WeightChoice["role"]) => {
    if (kg === null || kg === undefined || seen.has(round1(kg))) return;
    seen.add(round1(kg));
    out.push({ kg, role });
  };
  add(i?.empty_kg, "empty");
  for (const kg of [...new Set(i?.flown_kg ?? [])].sort((a, b) => a - b)) add(kg, "flown");
  add(i?.max_kg, "max");
  return out;
}

const round1 = (x: number) => Math.round(x * 10) / 10;

/** The weights flown, lightest and heaviest: figures outside them are extrapolated. */
export function flownSpan(i: Inputs | undefined): [number, number] | null {
  const kg = i?.flown_kg ?? [];
  return kg.length ? [Math.min(...kg), Math.max(...kg)] : null;
}

export interface WeightChart {
  x: Axis;
  y: Axis;
  line: string;
  points: { x: number; y: number; kg: number }[];
  px: (v: number) => number;
  py: (v: number) => number;
}

/** One figure against take-off weight: the line through the engine's weights, on round
 * axes from zero. */
export function weightChart(rows: Figures[], value: (f: Figures) => number, f: Frame): WeightChart | null {
  const usable = rows.filter((r) => Number.isFinite(value(r))).sort((a, b) => a.mass_kg - b.mass_kg);
  if (usable.length < 2) return null;
  const x = axis(usable.map((r) => r.mass_kg), 6);
  const y = axis(usable.map(value), 4, 0);
  const px = (v: number) => f.left + ((v - x.lo) / (x.hi - x.lo)) * (f.width - f.left - f.right);
  const py = (v: number) => f.height - f.bottom - ((v - y.lo) / (y.hi - y.lo)) * (f.height - f.top - f.bottom);
  const points = usable.map((r) => ({ x: px(r.mass_kg), y: py(value(r)), kg: r.mass_kg }));
  const line = points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  return { x, y, line, points, px, py };
}

/** A figure's 95% interval as "low–high", or null without one. */
export function interval(s: Spread | undefined, scale: (v: number) => string): string | null {
  return s ? `${scale(s.low)}–${scale(s.high)}` : null;
}

/** Whether the asked weight lies inside the weights flown: a figure there is measured,
 * outside it extrapolated. */
export function measuredAt(kg: number | null | undefined, span: [number, number] | null): boolean {
  return kg !== null && kg !== undefined && span !== null && kg >= span[0] - 0.05 && kg <= span[1] + 0.05;
}
