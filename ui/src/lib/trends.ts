// An airframe's trends as the engine's trend.* methods return them, and the pure
// helpers behind the Trends view (spec: Performance trends and ideal ranges; mock-up
// Trends): one point per flight, read against the metric's zones and against the
// airframe's own baseline, with every change marked on the same axis.

import { translate, type Locale } from "./i18n";
import { formatNumber, type Baseline, type Interval, type Zone } from "./report";

/** A payload group as the engine describes it: a band by hover thrust, or the label of
 * flights with no hover thrust (spec: Performance trends and ideal ranges, "Like with like"). */
export type GroupInfo =
  | { band: number; low: number; high: number; above_lightest: number; kg: [number, number] | null }
  | { label: string | null };

export type PayloadGroup = GroupInfo & { group: string; flights: number; labels: string[] };

export interface WeightCheck {
  flight: string;
  weighed_kg: number;
  estimated_kg: number;
  error_pct: number;
}

export interface PayloadGroups {
  airframe: string;
  lightest: number | null;
  kg_per_percent: number | null;
  latest: string | null;
  groups: PayloadGroup[];
  check: WeightCheck[];
}

export interface TrendCard {
  metric: string;
  segment: string;
  group: string; // 'band:<n>' or 'label:<text>'
  payload: string | null; // the latest flight's label, for display
  unit: string;
  value: number;
  zone: Zone | null;
  since_previous: number | null;
  since_baseline: number | null;
  baseline: Baseline | null;
  drift: boolean;
  sparkline: number[];
}

export interface TrendPoint {
  flight: string;
  utc_us: number | null;
  value: number;
  spread: Record<string, number>;
  zone: Zone | null;
  payload: string | null;
  group: string;
  kg: number | null; // estimated from hover thrust, once a flight was weighed
  takeoff_kg: number | null; // as weighed
  good: boolean;
}

export interface Series {
  airframe: string;
  metric: string;
  segment: string;
  group: string | null;
  payload: GroupInfo | null;
  unit: string;
  direction: "lower" | "higher" | "band";
  zones: Partial<Record<Zone, Interval[]>>;
  source: { kind: string; title: string; url?: string };
  points: TrendPoint[];
  baseline: Baseline | null;
  drift: { flights: string[]; since: string } | null;
  apart?: boolean; // in the platform view: away from its siblings
}

export type TrendEvent =
  | {
      kind: "parameter change" | "build change" | "ground station change";
      id: string;
      flight: string | null;
      origin: string;
      params: string[];
      // each change with its value before and after (canonical names and units) and its class
      changes?: { name: string; before: number | null; after: number | null; class: string | null }[];
    }
  | { kind: "build"; id: string; flight: string | null; number: number; reason: string | null }
  | { kind: "firmware"; flight: string; from: string; to: string }
  | { kind: "recommendation"; id: string; flight: string; param: string | null; proposed: number | null; grade: string };

/** A card's identity: one metric in one segment. */
export const cardKey = (c: { metric: string; segment: string }) => `${c.metric}@${c.segment}`;

/** Where each event sits on the chart: the index of the flight it first applies to.
 * Events on flights outside this series (another payload, no value) are left out. */
export function placeEvents(points: TrendPoint[], events: TrendEvent[]): { x: number; event: TrendEvent }[] {
  const at = new Map(points.map((p, i) => [p.flight, i]));
  const out: { x: number; event: TrendEvent }[] = [];
  for (const e of events) {
    const x = e.flight === null ? undefined : at.get(e.flight);
    if (x !== undefined) out.push({ x, event: e });
  }
  return out;
}

/** The chart's y range: every point, the baseline, and the zone edges near the data,
 * so the bands that matter show without one far edge flattening the line. */
export function yRange(s: Series): [number, number] {
  const values = s.points.map((p) => p.value);
  if (s.baseline) values.push(s.baseline.low, s.baseline.high);
  if (values.length === 0) return [0, 1];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const span = Math.max(hi - lo, Math.abs(hi) * 0.25) || 1; // how far "near" reaches
  for (const intervals of Object.values(s.zones)) {
    for (const [a, b] of intervals ?? []) {
      for (const edge of [a, b]) {
        if (edge !== null && edge > lo - span && edge < hi + span) {
          lo = Math.min(lo, edge);
          hi = Math.max(hi, edge);
        }
      }
    }
  }
  const pad = (hi - lo || 1) * 0.12;
  return [lo - pad, hi + pad];
}

/** The series as uPlot data: x is the flight's place in the series, then the values. */
export function chartData(s: Series): number[][] {
  return [s.points.map((_, i) => i), s.points.map((p) => p.value)];
}

/** Small multiples on shared axes: one y range for every airframe (their points and
 * the zone edges near them), the zone bands across it, and each airframe's line
 * across its own flights, in a w×h box. */
export function multiples(
  rows: Series[],
  w: number,
  h: number,
): { bands: { zone: Zone; y: number; h: number }[]; rows: { airframe: string; series: Series; path: string }[] } {
  const first = rows[0];
  if (!first) return { bands: [], rows: [] };
  const all: Series = { ...first, baseline: null, points: rows.flatMap((r) => r.points) };
  const [lo, hi] = yRange(all);
  const y = (v: number) => h - ((Math.min(Math.max(v, lo), hi) - lo) / (hi - lo)) * h;
  const bands: { zone: Zone; y: number; h: number }[] = [];
  for (const [zone, intervals] of Object.entries(first.zones) as [Zone, Interval[]][]) {
    for (const [a, b] of intervals) {
      const top = y(b ?? hi);
      const bottom = y(a ?? lo);
      if (bottom > top) bands.push({ zone, y: top, h: bottom - top });
    }
  }
  return {
    bands,
    rows: rows.map((r) => ({
      airframe: r.airframe,
      series: r,
      path: r.points
        .map((p, i) => {
          const x = r.points.length > 1 ? (i / (r.points.length - 1)) * w : w / 2;
          return `${i ? "L" : "M"}${x.toFixed(1)} ${y(p.value).toFixed(1)}`;
        })
        .join(" "),
    })),
  };
}

/** A short SVG path for a sparkline in a w×h box. */
export function sparkPath(values: number[], w = 120, h = 28): string {
  if (values.length < 2) return "";
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  return values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * (w - 4) + 2;
      const y = h - 2 - ((v - lo) / span) * (h - 4);
      return `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

/** How a payload group reads: its kilograms once a flight was weighed, else how much
 * heavier than the airframe's lightest flights; flights with no hover thrust by their label. */
export function groupName(g: GroupInfo, locale: Locale): string {
  if (!("band" in g)) return g.label ?? translate(locale, "trends.payload.none");
  if (g.kg) return translate(locale, "trends.payload.kg", { low: formatNumber(g.kg[0], locale), high: formatNumber(g.kg[1], locale) });
  if (g.band === 0) return translate(locale, "trends.payload.lightest");
  return translate(locale, "trends.payload.heavier", { percent: g.above_lightest });
}

/** A picker option: the group's name, then the labels its flights were imported with. */
export function groupOption(g: PayloadGroup, locale: Locale): string {
  const name = groupName(g, locale);
  const labels = "band" in g ? g.labels : [];
  return labels.length ? `${name} · ${labels.join(", ")}` : name;
}

/** The worst gap between a weighed flight and its weight estimated from the others'. */
export function worstCheck(check: WeightCheck[]): number | null {
  return check.length ? Math.max(...check.map((c) => Math.abs(c.error_pct))) : null;
}

/** A change since an earlier value, worse or better for this metric's direction. */
export function changeTone(delta: number | null, direction: "lower" | "higher" | "band"): "better" | "worse" | "same" {
  if (delta === null || delta === 0 || direction === "band") return "same";
  return (delta > 0) === (direction === "lower") ? "worse" : "better";
}
