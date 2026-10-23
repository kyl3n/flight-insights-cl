// The filter workbench's data as the engine's filter.workbench returns it, and the
// pure helpers behind its plots (spec: User interface, "Filter workbench"; mock-up
// Workbench). Every number shown comes from the engine; the UI only draws it.

export interface Evaluation {
  available: boolean;
  reason?: string;
  noise_db: number;
  lag_deg: number;
  notches: number;
  within_budget: boolean;
  centres_hz: number[];
  bandwidth_hz: number | null;
  lowpass_hz: number;
  spectrum_db: number[][]; // per axis
}

export interface Spectrogram {
  t_s: number[];
  freqs: number[];
  db: number[][]; // [time][freq]
  motor_hz?: (number | null)[];
  notch_hz?: (number | null)[];
}

export interface Bench {
  segment: "hover" | "flight";
  spans_s: [number, number][];
  seconds: number;
  rate_hz: number;
  instance: number;
  noise_band_hz: [number, number];
  lag_hz: number;
  notch_cap: number | null;
  tracking: { set: string; mode: number; source: string; model_centre_hz: number | null }[];
  rpm_reliable: boolean;
  freqs: number[];
  before_db: number[][];
  flown_db: number[][] | null;
  flown_from: "logged" | "model" | null;
  params: Record<string, number>;
  current: Evaluation;
  candidate?: Evaluation;
  candidate_params?: Record<string, number>;
  spectrogram: Spectrogram | null;
}

export const AXES = ["roll", "pitch", "yaw"] as const;
export type Axis = (typeof AXES)[number];

/** What the person changed from the flight's own values, as numbers the engine takes;
 * a value left empty or unchanged is not a change. */
export function changes(flown: Record<string, number>, edited: Record<string, string>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [name, raw] of Object.entries(edited)) {
    const text = raw.trim().replace(",", ".");
    if (text === "") continue;
    const v = Number(text);
    if (Number.isFinite(v) && v !== flown[name]) out[name] = v;
  }
  return out;
}

/** The series one plot shows for one axis: frequency, before, as flown, candidate. */
export function plotData(b: Bench, axis: Axis): [number[], ...(number | null)[][]] {
  const i = AXES.indexOf(axis);
  const empty = b.freqs.map(() => null);
  return [
    b.freqs,
    b.before_db[i] ?? empty,
    b.flown_db?.[i] ?? empty,
    b.candidate?.available ? (b.candidate.spectrum_db[i] ?? empty) : empty,
  ];
}

/** Change from the flight's own filters: negative noise and lag are better. */
export function delta(b: Bench): { noise_db: number; lag_deg: number } | null {
  const c = b.candidate;
  if (!c?.available) return null;
  return {
    noise_db: Math.round((c.noise_db - b.current.noise_db) * 10) / 10,
    lag_deg: Math.round((c.lag_deg - b.current.lag_deg) * 10) / 10,
  };
}

/** The dB range the spectrogram's colours span: robust to a few extreme cells. */
export function dbRange(db: number[][]): [number, number] {
  const all = db.flat().filter(Number.isFinite).sort((a, b) => a - b);
  if (all.length === 0) return [0, 1];
  const at = (q: number) => all[Math.min(all.length - 1, Math.floor(q * (all.length - 1)))] ?? 0;
  const lo = at(0.02);
  const hi = at(0.995);
  return hi > lo ? [lo, hi] : [lo, lo + 1];
}

/** A dark-to-bright colour map for the spectrogram: navy, teal, amber, white. */
const STOPS: [number, [number, number, number]][] = [
  [0, [12, 18, 32]],
  [0.35, [24, 92, 120]],
  [0.65, [214, 150, 48]],
  [1, [250, 246, 230]],
];

export function colour(t: number): [number, number, number] {
  const x = Math.min(Math.max(t, 0), 1);
  for (let k = 1; k < STOPS.length; k++) {
    const [p1, c1] = STOPS[k]!;
    const [p0, c0] = STOPS[k - 1]!;
    if (x <= p1) {
      const f = (x - p0) / (p1 - p0);
      return [0, 1, 2].map((j) => Math.round(c0[j]! + (c1[j]! - c0[j]!) * f)) as [number, number, number];
    }
  }
  return STOPS[STOPS.length - 1]![1];
}

/** The spectrogram as RGBA pixels, time across and frequency up (row 0 is the top,
 * the highest frequency), for a texture or ImageData. */
export function pixels(s: Spectrogram): { width: number; height: number; data: Uint8ClampedArray<ArrayBuffer> } {
  const [lo, hi] = dbRange(s.db);
  const width = s.t_s.length;
  const height = s.freqs.length;
  const data = new Uint8ClampedArray(new ArrayBuffer(width * height * 4));
  for (let x = 0; x < width; x++) {
    const column = s.db[x] ?? [];
    for (let y = 0; y < height; y++) {
      const v = column[height - 1 - y] ?? lo;
      const [r, g, b] = colour((v - lo) / (hi - lo));
      const o = (y * width + x) * 4;
      data[o] = r;
      data[o + 1] = g;
      data[o + 2] = b;
      data[o + 3] = 255;
    }
  }
  return { width, height, data };
}
