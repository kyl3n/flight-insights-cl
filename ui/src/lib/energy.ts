// A flight's energy, as the engine's flight.energy returns it, and the helpers behind the
// Energy section of the Propulsion view (spec: Propulsion and power, "Energy"; mock-up
// Propulsion). The numbers are the engine's; here they are only laid out: the parts of
// the energy as one bar, the phases as coloured spans of time, and the flight's points
// on its airframe's curve.

import type { MessageKey } from "./i18n";
import type { Envelope } from "./flightviews";
import { engineCall } from "./shell";
import type { Status } from "./status";

export interface Climb {
  wh_per_m: number;
  rate_ms: number;
  metres: number;
  above_hover_wh_per_m: number | null;
}

export interface Figures {
  used_wh: number;
  used_mah: number;
  capacity_mah: number | null;
  capacity_from: "measured" | "parameters" | "none";
  share_pct: number | null;
  flown_m: number;
  seconds: number;
  wh_per_km: number | null;
  cruise_wh_per_km: number | null;
  cruise_airspeed_ms: number | null;
  bridged_s: number;
  monitors: number;
  read_from: string;
  climb: Climb | null;
}

export interface Part {
  part: string; // a flight phase, "untiled", "avionics", "harness" or "heat"
  wh: number;
}

export interface Breakdown {
  parts: Part[];
  terminal_wh: number;
  cells_wh: number | null;
  split: boolean;
  split_why: string | null;
  heat_why: string | null;
}

export interface PhaseSpan {
  phase: string;
  start_s: number;
  end_s: number;
}

export interface CurvePoint {
  phase: string;
  airspeed_ms: number;
  climb_ms: number;
  power_w: number;
  seconds: number;
  tiles: number;
}

export interface Curve {
  airspeed_ms: number[];
  power_w: number[];
  low_w: number[] | null;
  high_w: number[] | null;
}

export interface Against {
  excess_pct: number | null;
  interval_pct: [number | null, number | null];
  by_phase_pct: Record<string, number | null>;
  threshold_pct: number | null;
  others: number;
}

export interface OnCurve {
  why: string | null;
  points: CurvePoint[];
  mass_kg: number | null;
  mass_from: string | null;
  density_ratio: number | null;
  wind_from: string | null;
  level: string | null;
  curve: Curve | null;
  hover: { measured_w: number; model_w: number; standard_w: number; model_standard_w: number } | null;
  against: Against | null;
}

export interface Fast {
  spans_s: [number, number][];
  seconds: number;
  speed_ms: number;
  top_ms: number;
  speed_from: "air" | "ground";
}

export interface Watched {
  metric: string;
  value: number;
  zone: "target" | "caution" | "out" | null;
  evidence: string;
}

export interface EnergyFinding {
  code: string;
  severity: string;
  params: Record<string, unknown>;
}

export interface EnergyView {
  flight_id: string;
  simulated: boolean;
  window_s: [number, number];
  armed_s: [number, number];
  figures: Figures;
  phase_s: Record<string, number>;
  breakdown: Breakdown;
  power: Envelope;
  phases: PhaseSpan[];
  fast: Fast | null;
  fast_why: string | null;
  on_curve: OnCurve;
  findings: EnergyFinding[];
  watch: { values: Record<string, unknown>; metrics: Watched[] } | null;
}

export function flightEnergy(flightId: string): Promise<EnergyView> {
  return engineCall<EnergyView>("flight.energy", { flight_id: flightId });
}

/** Each part's colour, readable on both themes; flight phases in blues, the losses in
 * greys, as on the mock-up. */
export const PART_COLOURS: Record<string, string> = {
  hover: "#4A9FD8",
  level: "#2F7FB8",
  climb: "#7FC4EE",
  descent: "#9FB8CC",
  manoeuvre: "#4A7FA3",
  low: "#7F93A8",
  untiled: "#B7C2CC",
  avionics: "#8A949E",
  harness: "#6E7883",
  heat: "#4A535D",
};

/** The order parts are drawn in: the flight phases, then where the rest went. */
export const PART_ORDER = ["hover", "level", "climb", "descent", "manoeuvre", "low", "untiled", "avionics", "harness", "heat"];

export const partKey = (part: string) => `energy.part.${part}` as MessageKey;

export interface BarPart {
  part: string;
  wh: number;
  share: number; // of the whole, 0 to 1
  from: number; // where it starts along the bar, 0 to 1
  colour: string;
}

/** The parts with energy in them, in order, laid along one bar adding up to what the
 * cells gave (or the terminals, without the heat). A part under a thousandth of the whole
 * is left out. */
export function bar(b: Breakdown): BarPart[] {
  const all = PART_ORDER.map((p) => b.parts.find((x) => x.part === p)).filter((x): x is Part => !!x && x.wh > 0);
  const total = all.reduce((s, p) => s + p.wh, 0);
  const parts = all.filter((p) => p.wh >= 0.001 * total); // under a thousandth is not worth a key
  const whole = parts.reduce((s, p) => s + p.wh, 0);
  let at = 0;
  return parts.map((p) => {
    const share = whole > 0 ? p.wh / whole : 0;
    const out = { part: p.part, wh: p.wh, share, from: at, colour: PART_COLOURS[p.part] ?? "#8A949E" };
    at += share;
    return out;
  });
}

/** The phases as coloured spans of log time, for the power plot. */
export function shaded(phases: PhaseSpan[]): { start: number; end: number; colour: string }[] {
  return phases.map((p) => ({ start: p.start_s, end: p.end_s, colour: PART_COLOURS[p.phase] ?? "#8A949E" }));
}

/** How the flight sits against its airframe's model: over it beyond the threshold the
 * airframe's other flights set, or within it. */
export function standing(a: Against | null, findings: EnergyFinding[]): Status | null {
  if (findings.length) return "caution";
  if (!a || a.excess_pct === null || a.threshold_pct === null) return null;
  return Math.abs(a.excess_pct) > a.threshold_pct ? "caution" : "ok";
}

/** Minutes and seconds, as "14:40". */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export interface Axis {
  lo: number;
  hi: number;
  ticks: number[];
}

/** A round axis over the values: steps of 1, 2 or 5 times a power of ten, about `n` of
 * them, from a tick at or under the lowest to one at or over the highest. */
export function axis(values: number[], n = 4, floor: number | null = null): Axis {
  const finite = values.filter(Number.isFinite);
  let lo = finite.length ? Math.min(...finite) : 0;
  let hi = finite.length ? Math.max(...finite) : 1;
  if (floor !== null) lo = Math.min(lo, floor);
  if (hi <= lo) hi = lo + 1;
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((k) => k * mag).find((s) => s >= raw) ?? 10 * mag;
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v / step) * step);
  return { lo, hi, ticks };
}

export interface Frame {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export interface CurveChart {
  x: Axis;
  y: Axis;
  line: string; // SVG path of the model
  band: string | null; // its 95% band, closed
  points: { x: number; y: number; phase: string; p: CurvePoint }[];
  flown: number | null; // where the fastest point is: beyond it the curve is extrapolated
  px: (v: number) => number;
  py: (v: number) => number;
}

/** The airframe's curve with its band and the flight's hover and level points, laid out
 * on one frame. */
export function curveChart(c: OnCurve, f: Frame): CurveChart | null {
  if (!c.curve) return null;
  const shown = c.points.filter((p) => p.phase === "hover" || p.phase === "level");
  // the power axis is set by what was flown: far beyond it the band widens without bound
  const fastest = shown.length ? Math.max(...shown.map((p) => p.airspeed_ms)) : null;
  const near = c.curve.airspeed_ms.map((v) => fastest === null || v <= fastest + 2);
  const within = (xs: number[] | null) => (xs ?? []).filter((_, i) => near[i]);
  const powers = [...within(c.curve.power_w), ...within(c.curve.low_w), ...within(c.curve.high_w), ...shown.map((p) => p.power_w)];
  const x = axis(c.curve.airspeed_ms, 4, 0);
  const y = axis(powers, 4);
  const px = (v: number) => f.left + ((v - x.lo) / (x.hi - x.lo)) * (f.width - f.left - f.right);
  const py = (v: number) => f.height - f.bottom - ((v - y.lo) / (y.hi - y.lo)) * (f.height - f.top - f.bottom);
  const path = (xs: number[], ys: number[]) => xs.map((v, i) => `${i ? "L" : "M"}${px(v).toFixed(1)} ${py(ys[i]!).toFixed(1)}`).join(" ");
  const speeds = c.curve.airspeed_ms;
  let band: string | null = null;
  if (c.curve.low_w && c.curve.high_w) {
    const back = speeds.map((v, i) => `L${px(v).toFixed(1)} ${py(c.curve!.low_w![i]!).toFixed(1)}`).reverse();
    band = `${path(speeds, c.curve.high_w)} ${back.join(" ")} Z`;
  }
  return {
    x,
    y,
    line: path(speeds, c.curve.power_w),
    band,
    points: shown.map((p) => ({ x: px(p.airspeed_ms), y: py(p.power_w), phase: p.phase, p })),
    flown: fastest === null ? null : px(fastest),
    px,
    py,
  };
}

/** The watched metrics in the mock-up's order, the ones the engine measured. */
export const WATCH_ORDER = [
  "fast.vibe_p95",
  "fast.clips",
  "fast.output_max",
  "fast.saturation_pct",
  "fast.esc_temp_max_c",
  "fast.current_max_a",
  "fast.sag_v",
  "fast.lean_share",
  "fast.attitude_error_deg",
  "fast.velocity_error_ms",
  "fast.height_loss_m",
  "fast.thrust_loss",
];

export function watched(rows: Watched[]): Watched[] {
  return WATCH_ORDER.map((m) => rows.find((r) => r.metric === m)).filter((r): r is Watched => !!r);
}
