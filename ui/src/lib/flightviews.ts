// The propulsion and power view and the system and buses view of one flight, as the
// engine's flight.propulsion and flight.system return them, and the pure helpers
// behind their plots (spec: User interface, "Propulsion and power", "System and
// buses"). Traces arrive reduced for display: each point is one time bin's lowest and
// highest sample, so spikes survive. Units are the log's own.

export interface Envelope {
  t: number[]; // seconds, log time
  lo: number[];
  hi: number[];
}

interface Context {
  window_s: [number, number]; // the whole flight, with a margin either side
  shown_s: [number, number]; // the stretch these traces cover
  armed_s: [number, number];
  hover_s: [number, number][];
  modes: { t: number; mode: string }[];
  simulated: boolean;
  units: Record<string, string>;
}

export interface MotorRow {
  number: number;
  channel: number;
  output?: Envelope;
  hover_output?: number | null;
  rpm?: Envelope;
  hover_rpm?: number | null;
  temp?: Envelope;
  hover_temp?: number | null;
  peak_temp?: number | null;
  curr?: Envelope;
  hover_curr?: number | null;
  volt?: Envelope;
  hover_volt?: number | null;
}

export interface Battery {
  volt: Envelope;
  curr: Envelope;
  power?: Envelope;
  res?: Envelope;
  resting_volt: number | null;
  lowest_volt: number | null;
}

export interface PropulsionView extends Context {
  saturation_pct: number;
  motors: MotorRow[];
  battery: Battery | null;
}

export interface FlightError {
  t: number;
  subsystem: string;
  code: number;
  meaning: string;
}

export interface SystemView extends Context {
  load?: Envelope;
  long_loops?: Envelope;
  max_loop?: Envelope;
  loop_rate?: Envelope;
  loop_period_us: number | null;
  log_dropped?: Envelope;
  errors: FlightError[];
}

export interface Trace {
  label: string;
  env: Envelope;
  colour: string;
}

/** Several traces on one time axis for uPlot: the union of their bin times, then each
 * trace's low and high rows, with gaps where a trace has no bin. */
export function align(traces: Trace[]): (number | null)[][] {
  const times = [...new Set(traces.flatMap((tr) => tr.env.t))].sort((a, b) => a - b);
  const rows: (number | null)[][] = [times];
  for (const tr of traces) {
    const at = new Map(tr.env.t.map((t, i) => [t, i]));
    const pick = (values: number[]) =>
      times.map((t) => {
        const i = at.get(t);
        return i === undefined ? null : (values[i] ?? null);
      });
    rows.push(pick(tr.env.lo), pick(tr.env.hi));
  }
  return rows;
}

/** The x range for a segment choice: the whole window, or the hover windows' span. */
export function zoomTo(view: Pick<Context, "window_s" | "hover_s">, segment: "flight" | "hover"): [number, number] {
  if (segment === "hover" && view.hover_s.length) {
    const first = view.hover_s[0]!;
    const last = view.hover_s[view.hover_s.length - 1]!;
    const pad = Math.max((last[1] - first[0]) * 0.05, 1);
    return [first[0] - pad, last[1] + pad];
  }
  return view.window_s;
}

/** The motor that stands out on a measure: the highest value, when it is clearly above
 * its siblings' median (by `margin`, a share of that median). */
export function standout(
  rows: MotorRow[],
  pick: (m: MotorRow) => number | null | undefined,
  margin = 0.05,
): number | null {
  const values = rows.map((m) => ({ n: m.number, v: pick(m) })).filter((x): x is { n: number; v: number } => typeof x.v === "number");
  if (values.length < 3) return null;
  const top = values.reduce((a, b) => (b.v > a.v ? b : a));
  const others = values.filter((x) => x !== top).map((x) => x.v).sort((a, b) => a - b);
  const median = others[Math.floor(others.length / 2)] ?? top.v;
  return top.v - median > Math.abs(median) * margin ? top.n : null;
}

/** A fixed set of distinguishable colours for motors, by motor number. */
const MOTOR_COLOURS = ["#4A9FD8", "#E0952B", "#2FA877", "#A86FE0", "#E0605C", "#C9A227", "#2BA3A3", "#D9629B", "#7F93A8", "#7FAF3A", "#B07A4A", "#6F7FE0"]; // readable on both themes
export const motorColour = (n: number) => MOTOR_COLOURS[(n - 1) % MOTOR_COLOURS.length]!;

/** Errors grouped for the table: one row per subsystem and code, with its first time
 * and how many times it was logged. A code of 0 means resolved and is listed too. */
export function groupErrors(errors: FlightError[]): (FlightError & { count: number })[] {
  const out = new Map<string, FlightError & { count: number }>();
  for (const e of errors) {
    const key = `${e.subsystem}|${e.code}`;
    const seen = out.get(key);
    if (seen) seen.count += 1;
    else out.set(key, { ...e, count: 1 });
  }
  return [...out.values()].sort((a, b) => a.t - b.t);
}
