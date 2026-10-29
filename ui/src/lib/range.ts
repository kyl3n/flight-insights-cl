// An airframe's range, flight time and speeds, as the engine's airframe.performance returns
// them, and the helpers behind the Range and speed screen (spec: Power and efficiency
// analysis, "Range, endurance and speed"; mock-up Performance). The figures are the
// engine's: worked out on this laptop from the airframe's own flights. Here they are
// only laid out.

import { axis, type Axis, type Frame } from "./energy";
import type { Builds } from "./bench";
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
  speed_goal?: "efficient" | "fastest";
}

export interface Check {
  name: string; // lean, thrust, current, esc_temp, output, vibration, attitude, velocity
  value: number;
  limit: number;
  holds: boolean;
  how: "model" | "trend";
}

export interface BuildUp {
  verified_ms: number | null;
  verified_flight: string | null;
  next_ms: number | null;
  checks: Check[];
  missing: string[];
  binding: string | null;
  offered: boolean;
}

/** Each check's unit, as the engine measures it. */
export const CHECK_UNITS: Record<string, string> = {
  lean: "%",
  thrust: "deg",
  current: "A",
  esc_temp: "degC",
  output: "%",
  vibration: "m/s/s",
  attitude: "deg",
  velocity: "m/s",
};

/** How much of its limit a check uses, 0 to 1 and beyond. */
export function used(c: Check): number {
  return c.limit > 0 ? c.value / c.limit : 1;
}

/** A polar of the radius by direction relative to the wind: up is out into the wind (the
 * engine's angle 0 is out with the wind behind, so it is drawn at the bottom). */
export function polar(by: [number, number][], cx: number, cy: number, radius: number, most: number): string {
  if (!by.length || most <= 0) return "";
  const pts = by.map(([deg, m]) => {
    const r = (m / most) * radius;
    const a = ((deg + 180) * Math.PI) / 180;
    return `${(cx + r * Math.sin(a)).toFixed(1)} ${(cy - r * Math.cos(a)).toFixed(1)}`;
  });
  return `M${pts.join(" L")} Z`;
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
  curves?: Curves;
  wp_spd_ms?: number | null;
  build_up?: BuildUp;
  radius_wind?: { wind_ms: number; worst_m: number; across_m: number | null }[];
  headwind?: { wind_ms: number; airspeed: number | null; ground?: number; power?: number; wh_per_km?: number }[];
  failsafe?: Failsafe;
  battery?: Battery;
  what_ifs?: WhatIf[];
  builds?: Builds;
}

/** One of the airframe's packs on the flights it flew alone, in flight order. */
export interface PackHistory {
  code: string;
  rated_mah: number | null;
  flights: { capacity_mah: number | null; resistance_mohm: number }[];
}

export interface Battery {
  usable: { power_w: number; energy_wh: number; ends: End }[];
  loads: Record<"hover" | "endurance" | "range", number>;
  aged_capacity: number; // share of rated at end of life
  aged_resistance: number; // multiple of new at end of life
  caution_resistance?: number; // multiple of new for the early caution
  packs: PackHistory[];
}

/** One lever alone, by the rules' step, at the weight picked: what each figure gains. */
export interface WhatIf {
  lever: "mass" | "drag" | "avionics" | "capacity";
  change: number; // kg, m^2 of flat plate, W, or Ah
  hover_s: number;
  endurance_s: number;
  range_m: number;
  radius_m: number;
  share: number | null; // the change as a share of what it changes
  basis: "measured" | "theory";
}

/** A pack's capacity as a share of its rated, and its resistance as a multiple of new (the
 * median of its first flights, as the engine compares them), flight by flight. */
export function packShares(p: PackHistory): { capacity: (number | null)[]; resistance: number[] } {
  const rated = p.rated_mah && p.rated_mah > 0 ? p.rated_mah : null;
  const capacity = p.flights.map((f) => (rated !== null && f.capacity_mah !== null ? f.capacity_mah / rated : null));
  const ohms = p.flights.map((f) => f.resistance_mohm);
  const k = Math.min(3, Math.max(1, Math.floor(ohms.length / 2)));
  const first = [...ohms.slice(0, k)].sort((a, b) => a - b);
  const mid = first.length % 2 ? first[(first.length - 1) / 2]! : (first[first.length / 2 - 1]! + first[first.length / 2]!) / 2;
  return { capacity, resistance: ohms.map((r) => (mid > 0 ? r / mid : 1)) };
}

/** A what-if's gains as shares of the figures at the weight picked. */
export function whatIfShares(w: WhatIf, f: Figures): { range: number; endurance: number } {
  return { range: f.range_m > 0 ? w.range_m / f.range_m : 0, endurance: f.endurance_s > 0 ? w.endurance_s / f.endurance_s : 0 };
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

export interface PowerCurve {
  mass_kg: number;
  power_w: number[];
  low_w: number[];
  high_w: number[];
}

export interface Curves {
  airspeed_ms: number[];
  top_ms: number;
  power: PowerCurve[];
  wh_per_km: Record<string, (number | null)[]>;
  flight_s: number[];
  points: { flight: number; mass_kg: number; phase: string; airspeed_ms: number; power_w: number; seconds: number }[];
}

export interface Plot {
  x: Axis;
  y: Axis;
  px: (v: number) => number;
  py: (v: number) => number;
}

/** Round axes over the values, and the frame's scales for them. */
export function plot(xs: number[], ys: number[], f: Frame, yFloor: number | null = 0, xFloor: number | null = 0): Plot {
  const x = axis(xs, 5, xFloor);
  const y = axis(ys.filter(Number.isFinite), 4, yFloor);
  return {
    x,
    y,
    px: (v) => f.left + ((v - x.lo) / (x.hi - x.lo)) * (f.width - f.left - f.right),
    py: (v) => f.height - f.bottom - ((v - y.lo) / (y.hi - y.lo)) * (f.height - f.top - f.bottom),
  };
}

/** A line split where it passes the speeds flown: solid up to `top`, dashed beyond. Gaps
 * (nulls) break it. */
export function split(xs: number[], ys: (number | null)[], top: number, p: Plot): { solid: string; beyond: string } {
  const parts = { solid: [] as string[], beyond: [] as string[] };
  let prev: string | null = null;
  let prevKind: "solid" | "beyond" | null = null;
  xs.forEach((x, i) => {
    const y = ys[i];
    if (y === null || y === undefined || !Number.isFinite(y)) {
      prev = null;
      prevKind = null;
      return;
    }
    const pt = `${p.px(x).toFixed(1)} ${p.py(y).toFixed(1)}`;
    const kind = x <= top + 1e-9 ? "solid" : "beyond";
    if (prev !== null && prevKind === kind) parts[kind].push(`L${pt}`);
    else parts[kind].push(prev !== null ? `M${prev} L${pt}` : `M${pt}`);
    prev = pt;
    prevKind = kind;
  });
  return { solid: parts.solid.join(" "), beyond: parts.beyond.join(" ") };
}

/** A band between two lines, closed. */
export function band(xs: number[], low: number[], high: number[], p: Plot): string {
  const up = xs.map((x, i) => `${i ? "L" : "M"}${p.px(x).toFixed(1)} ${p.py(high[i]!).toFixed(1)}`);
  const down = xs.map((x, i) => `L${p.px(x).toFixed(1)} ${p.py(low[i]!).toFixed(1)}`).reverse();
  return `${up.join(" ")} ${down.join(" ")} Z`;
}

/** The value of a curve at a speed, read between its grid points. */
export function at(xs: number[], ys: (number | null)[], v: number): number | null {
  for (let i = 1; i < xs.length; i++) {
    if (v <= xs[i]!) {
      const a = ys[i - 1], b = ys[i];
      if (a === null || a === undefined || b === null || b === undefined) return null;
      const t = (v - xs[i - 1]!) / (xs[i]! - xs[i - 1]!);
      return a + t * (b - a);
    }
  }
  return null;
}

/** The speed of least value along a curve, within the speeds flown. */
export function least(xs: number[], ys: (number | null)[], top: number): { v: number; y: number } | null {
  let best: { v: number; y: number } | null = null;
  xs.forEach((x, i) => {
    const y = ys[i];
    if (x <= top + 1e-9 && y !== null && y !== undefined && Number.isFinite(y) && (!best || y < best.y)) best = { v: x, y };
  });
  return best;
}
