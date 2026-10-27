// The flight report's data and the helpers that turn it into what the screen shows
// (spec: User interface, "Flight report"; Explanations for every metric and data point).
// Wording comes from the engine's registry in both languages; nothing here invents
// a threshold or an explanation.

import { CATALOGS, translate, type Locale, type MessageKey } from "./i18n";
import type { Plan } from "./plan";
import type { Status } from "./status";

export type Severity = "info" | "caution" | "out" | "safety";
export type Zone = "target" | "caution" | "out";
export type Direction = "lower" | "higher" | "band";
export type Text = Record<Locale, string>;
export type Interval = [number | null, number | null];

export interface MetricInfo {
  name: Text;
  unit: string;
  direction: Direction;
  definition: Text;
  method: Text;
  messages: string[];
  zones: Partial<Record<Zone, Interval[]>>;
  source: { kind: string; title: string; url?: string };
}

export interface FindingInfo {
  title: Text;
  action: Text;
  stage: string;
}

/** A configurable subsystem, with what covers it (spec: Configuration review, "Better
 * options"): its better options, the other findings that check it, and for one the app
 * does not cover yet, the phase its own analysis comes in. */
export interface CoverageInfo {
  title: Text;
  options: string[];
  also?: string[];
  phase?: number | null;
  covered: boolean;
}

export interface Registry {
  version: string;
  metrics: Record<string, MetricInfo>;
  findings: Record<string, FindingInfo>;
  coverage?: Record<string, CoverageInfo>;
}

export interface MetricValue {
  metric: string;
  segment: string;
  value: number;
  spread: Record<string, number>;
  zone: Zone | null;
  evidence: string;
}

export interface Change {
  param: string;
  current: number | null;
  proposed: number | null;
  stage: string;
}

export interface FindingRow {
  id: string;
  analyzer: string;
  code: string;
  severity: Severity;
  confidence: "low" | "medium" | "high";
  params: Record<string, unknown>;
  evidence: string[];
  changes: Change[];
  stage: string | null;
  /** Context the finding is read against, such as elevated space weather during the
   * flight (engine weather/context.py): a note only, nothing about it changes. */
  context?: { kp_max: number; note: Text };
}

export interface EvidenceRow {
  id: string;
  kind: string;
  summary: string;
  messages: string[];
  fields: string[];
  segment: { kind: string; start_us: number; end_us: number } | null;
  values: Record<string, unknown>;
}

export interface Analysis {
  run: string;
  flight: string;
  status: "complete" | "partial";
  current: boolean;
  analyzers: Record<string, { version: number; skipped: string | null }>;
  metrics: MetricValue[];
  findings: FindingRow[];
  evidence: EvidenceRow[];
}

/** A parameter file's configuration review (review.file): findings and a plan, with no
 * flight behind them. */
export interface FileReview {
  file: string;
  kind: string;
  vehicle: string;
  firmware: string;
  firmware_from: "airframe" | "newest";
  defaults_from: "airframe" | "fleet" | null;
  parameters: number;
  findings: FindingRow[];
  evidence: EvidenceRow[];
  plan: Plan;
}

export interface FlightRow {
  id: string;
  airframe_id: string;
  airframe: string;
  utc_us: number | null;
  duration_s: number | null;
  firmware: string | null;
  payload: string | null;
  modes: string[];
  analysis: { status: string; counts: Partial<Record<Severity, number>>; worst: Severity | null } | null;
}

export interface Baseline {
  low: number;
  high: number;
  median: number;
  flights: number;
}

export const SEVERITY_ORDER: Severity[] = ["safety", "out", "caution", "info"];
export const STAGES = [
  "mechanical", "evidence", "setup", "filters", "rate tune", "autotune", "feel",
  "navigation", "operations",
] as const;

/** The status shape and word a severity is shown with. */
export function severityLook(severity: Severity): { status: Status; label: MessageKey } {
  switch (severity) {
    case "safety":
      return { status: "out", label: "severity.safety" };
    case "out":
      return { status: "out", label: "severity.out" };
    case "caution":
      return { status: "caution", label: "severity.caution" };
    default:
      return { status: "info", label: "severity.info" };
  }
}

export function zoneLook(zone: Zone | null): { status: Status; label: MessageKey } {
  switch (zone) {
    case "target":
      return { status: "ok", label: "zone.target" };
    case "caution":
      return { status: "caution", label: "zone.caution" };
    case "out":
      return { status: "out", label: "zone.out" };
    default:
      return { status: "info", label: "zone.none" };
  }
}

export function stageKey(stage: string): MessageKey {
  return `stage.${stage.replace(" ", "_")}` as MessageKey;
}

/** Findings most urgent first, then in the tuning order of their stage. */
export function sortFindings(findings: FindingRow[]): FindingRow[] {
  const stage = (f: FindingRow) => {
    const i = STAGES.indexOf((f.stage ?? "") as (typeof STAGES)[number]);
    return i < 0 ? STAGES.length : i;
  };
  return [...findings].sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) || stage(a) - stage(b),
  );
}

/** A number as people read it: three significant figures at most, no trailing zeros. */
export function formatNumber(value: number, locale: Locale = "en"): string {
  if (!Number.isFinite(value)) return "–";
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : abs >= 1 ? 2 : 3;
  return new Intl.NumberFormat(locale === "uk" ? "uk-UA" : "en-GB", {
    maximumFractionDigits: digits,
  }).format(value);
}

// The registry writes units in ASCII; people read them like this, in their language
// (Ukrainian writes SI symbols in Cyrillic: В, Вт, Гц).
const UNIT_TEXT: Record<Locale, Record<string, string>> = {
  en: { degC: "°C", mOhm: "mΩ", Ohm: "Ω", "W.h": "Wh", "A/krpm^2": "A/krpm²", us: "µs" },
  uk: {
    degC: "°C", mOhm: "мОм", "W.h": "Вт·год", "A/krpm^2": "А/(тис. об/хв)²", W: "Вт", V: "В",
    A: "А", s: "с", ms: "мс", Hz: "Гц", dB: "дБ", "B/s": "Б/с", "m/s/s": "м/с²", Ohm: "Ом", rpm: "об/хв",
    m: "м", "m/s": "м/с", hPa: "гПа", us: "мкс",
  },
};

export function unitText(unit: string, locale: Locale = "en"): string {
  return UNIT_TEXT[locale][unit] ?? UNIT_TEXT.en[unit] ?? unit;
}

/** A value with its unit. */
export function formatValue(value: number, unit: string, locale: Locale = "en"): string {
  const text = formatNumber(value, locale);
  if (!unit || unit === "count" || unit === "ratio") return text;
  return unit === "%" ? `${text}%` : `${text} ${unitText(unit, locale)}`;
}

function fillValue(v: unknown, locale: Locale): string {
  if (typeof v === "number") return formatNumber(v, locale);
  if (Array.isArray(v)) return v.map((x) => fillValue(x, locale)).join(", ");
  if (v === null || v === undefined) return "–";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** A registry text with the finding's {placeholders} filled; a missing one stays. */
export function fill(template: string, params: Record<string, unknown>, locale: Locale): string {
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in params ? fillValue(params[name], locale) : whole,
  );
}

export function findingTitle(reg: Registry, f: Pick<FindingRow, "code" | "params">, locale: Locale): string {
  const text = reg.findings[f.code];
  return text ? fill(text.title[locale], f.params, locale) : f.code;
}

export function findingAction(reg: Registry, f: Pick<FindingRow, "code" | "params">, locale: Locale): string {
  const text = reg.findings[f.code];
  return text ? fill(text.action[locale], f.params, locale) : "";
}

export interface ScaleBand {
  zone: Zone;
  from: number; // fraction of the scale, 0 to 1
  to: number;
}

/** The zones laid out on one scale with the value on it, for the small bar under a
 * value: open ends are closed a little beyond the nearest edge or the value. */
export function scale(metric: MetricInfo, value: number): { bands: ScaleBand[]; at: number } | null {
  const edges: number[] = [value];
  for (const intervals of Object.values(metric.zones)) {
    for (const [lo, hi] of intervals ?? []) {
      if (lo !== null) edges.push(lo);
      if (hi !== null) edges.push(hi);
    }
  }
  if (edges.length < 2) return null;
  let min = Math.min(...edges);
  let max = Math.max(...edges);
  const pad = (max - min || Math.abs(max) || 1) * 0.15;
  min -= pad;
  max += pad;
  const at = (v: number) => (v - min) / (max - min);
  const bands: ScaleBand[] = [];
  for (const [zone, intervals] of Object.entries(metric.zones) as [Zone, Interval[]][]) {
    for (const [lo, hi] of intervals) {
      bands.push({ zone, from: at(lo ?? min), to: at(hi ?? max) });
    }
  }
  bands.sort((a, b) => a.from - b.from);
  return { bands, at: at(value) };
}

/** A zone's interval as text: "below 30", "30–60", "60 and above". */
export function intervalText(
  [lo, hi]: Interval,
  unit: string,
  locale: Locale,
  words: { below: string; above: string },
): string {
  const v = (x: number) => formatValue(x, unit, locale);
  if (lo === null && hi === null) return "–";
  if (lo === null) return `${words.below} ${v(hi as number)}`;
  if (hi === null) return `${v(lo)} ${words.above}`;
  return `${formatNumber(lo, locale)}–${v(hi)}`;
}

export function formatDuration(seconds: number | null): { min: number; s: number } | null {
  if (seconds === null || !Number.isFinite(seconds)) return null;
  const total = Math.round(seconds);
  return { min: Math.floor(total / 60), s: total % 60 };
}

export function formatTakeoff(utcUs: number | null, locale: Locale): string | null {
  if (utcUs === null) return null;
  return new Intl.DateTimeFormat(locale === "uk" ? "uk-UA" : "en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(utcUs / 1000));
}

/** The subsystem a metric belongs to: the part of its id before the first dot. */
export function subsystem(metricId: string): string {
  return metricId.split(".")[0] ?? metricId;
}

/** A subsystem's name in the interface language; an unknown one shows its key. */
export function subsystemName(key: string, locale: Locale): string {
  const k = `subsystem.${key}`;
  return k in CATALOGS.en ? translate(locale, k as MessageKey) : key;
}

/** Metric values grouped by subsystem, in registry order inside each group. */
export function groupMetrics(values: MetricValue[], reg: Registry): [string, MetricValue[]][] {
  const known = values.filter((v) => v.metric in reg.metrics);
  const groups = new Map<string, MetricValue[]>();
  for (const v of known) {
    const key = subsystem(v.metric);
    groups.set(key, [...(groups.get(key) ?? []), v]);
  }
  return [...groups.entries()];
}
