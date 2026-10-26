// The Tune view of one flight, as the engine's flight.tune returns it, and the pure
// helpers behind it (spec: User interface, "Tune"; mock-up Tune): tracking error
// against a flight of the same payload group, the responses of one axis, the stick
// inputs counted and left out, and whether the plan improved the tune. Every verdict
// is the engine's, judged as the gate's checks judge; nothing here sets a threshold.

import { translate, type Locale, type MessageKey } from "./i18n";
import type { ProcedureStep } from "./plan";
import { formatNumber, type Registry, type Text } from "./report";
import type { Status } from "./status";
import type { PayloadGroup } from "./trends";

export type Axis = "roll" | "pitch" | "yaw";
export const AXES: Axis[] = ["roll", "pitch", "yaw"];
export type Family = "attitude_error" | "tracking_error";
export type Verdict = "better" | "worse" | "unchanged";
export type Reason = "log gap" | "mode" | "mode change" | "low" | "saturated" | "no stick";
export const REASONS: Reason[] = ["log gap", "mode", "mode change", "low", "saturated", "no stick"];

export interface Measure {
  value: number;
  low?: number;
  high?: number;
  events?: number;
}

export interface Row {
  metric: string;
  family: Family;
  axis: Axis;
  this: Measure | null;
  compare: Measure | null;
  change?: number | null; // (this - compare) / compare
  apart?: boolean | null; // the 95% intervals do not overlap; null without intervals
  verdict?: Verdict;
}

export interface InputEvent {
  start_s: number; // from arming
  end_s: number;
  counted: boolean;
  reason: Reason | null;
  stick: number | null; // the largest deflection, a share of the stick's throw
}

export interface Inputs {
  why?: string;
  armed_s?: number;
  rate_hz?: number;
  axes?: Partial<Record<Axis, InputEvent[]>>;
  stick_median?: number | null;
}

export interface Curves {
  why?: string;
  hz?: number;
  seconds?: number;
  frequency?: { f: number[]; db: number[]; measured_to_hz: number; bandwidth_hz: number | null };
  step?: { t: number[]; y: number[]; low?: number[]; high?: number[] } | null;
}

export interface Side {
  id: string;
  utc_us: number | null;
  group: string;
  payload: string | null;
  why?: "before_change" | "same_tune" | "chosen";
  inputs?: Inputs;
  curves?: Curves;
}

export interface Candidate extends Side {
  before: boolean;
  same_tune: boolean;
}

export interface GateBand {
  group: string;
  before_flight: string;
  before: number;
  after_flight?: string;
  after?: number;
  change?: number; // the improvement, as a share: positive is better
  verdict: Verdict | "waiting";
}

export interface GateCheck {
  check: string;
  metric?: string;
  by?: number;
  required?: boolean;
  below?: number;
  above?: number;
  max_ratio?: number;
  max_above?: number;
  passed: boolean | null;
  worse?: boolean;
  bands?: (GateBand | string)[];
  shifts?: { metric: string; group: string; flight: string; cause: string | null }[];
  light?: number;
  heavy?: number;
}

export interface Gate {
  id: string;
  status: string;
  applied_flight: string | null;
  graded_flight: string | null;
  pending: boolean;
  checks: GateCheck[];
  changes: { param: string; value: number; status: string }[];
}

export interface TuneView {
  flight: string;
  group: string;
  own_group: string;
  groups: (PayloadGroup & { tune_checks: number })[];
  axis: Axis;
  this: Side | null;
  compare: Side | null;
  candidates: Candidate[];
  needs: Partial<Record<Family, { check: string; by: number }>>;
  rows: Row[];
  figures: Record<string, { this: Measure | null; compare: Measure | null }>;
  gate: Gate | null;
  card: { kind: ProcedureStep["kind"]; text: Text }[];
  labels: Record<string, { utc_us: number | null; group: string; payload: string | null }>;
}

/** A change as a signed percentage: "−31%", "+3%". */
export function formatChange(change: number, locale: Locale): string {
  const pct = Math.round(change * 100);
  const sign = pct < 0 ? "−" : pct > 0 ? "+" : "±";
  return `${sign}${formatNumber(Math.abs(pct), locale)}%`;
}

/** How a row reads: the change, then whether the intervals are apart and the change
 * is as large as the gate asks. The attitude error is the tune's measure, so a change
 * short of that is a caution; the rate error is its guard, fine unless clearly worse. */
export function rowLook(
  row: Row,
  by: number | undefined,
  locale: Locale,
): { status: Status; text: string } | null {
  if (row.change === undefined || row.change === null || !row.verdict) return null;
  const change = formatChange(row.change, locale);
  const need = by === undefined ? "" : `${formatNumber(Math.round(by * 100), locale)}%`;
  const key: MessageKey =
    row.apart === false ? "tune.verdict.within"
    : by !== undefined && Math.abs(row.change) < by ? "tune.verdict.small"
    : row.apart === true ? "tune.verdict.apart"
    : "tune.verdict.noInterval";
  const status: Status =
    row.verdict === "better" ? "ok"
    : row.verdict === "worse" ? "out"
    : row.family === "tracking_error" ? "ok"
    : "caution";
  return { status, text: translate(locale, key, { change, need }) };
}

/** The top of a dot-and-interval scale: the largest value or interval end, rounded up. */
export function scaleTop(measures: (Measure | null)[]): number {
  const top = Math.max(0, ...measures.flatMap((m) => (m ? [m.high ?? m.value, m.value] : [])));
  if (top <= 0) return 1;
  const step = 10 ** Math.floor(Math.log10(top));
  for (const k of [1, 2, 2.5, 5, 10]) if (k * step >= top) return k * step;
  return 10 * step;
}

/** A curve of a response plot; `tone` is a CSS custom property, `low` and `high` an
 * interval drawn as a band around it. */
export interface Curve {
  label: string;
  x: number[];
  y: number[];
  tone: string;
  dash?: number[];
  low?: number[];
  high?: number[];
}

/** A theme colour (#rgb or #rrggbb) with an opacity, for a canvas fill. */
export function withAlpha(colour: string, alpha: number): string {
  const hex = colour.trim().replace(/^#/, "");
  const full = hex.length === 3 ? [...hex].map((c) => c + c).join("") : hex;
  if (!/^[0-9a-f]{6}$/i.test(full)) return `rgba(128, 128, 128, ${alpha})`;
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Curves on one shared x axis for uPlot: the union of their x values, each series
 * null where it has no sample (a flight's frequency response stops at its own band). */
export function alignCurves(curves: { x: number[]; y: (number | null)[] }[]): (number | null)[][] {
  const xs = [...new Set(curves.flatMap((c) => c.x))].sort((a, b) => a - b);
  const rows: (number | null)[][] = [xs];
  for (const c of curves) {
    const at = new Map(c.x.map((x, i) => [x, c.y[i] ?? null]));
    rows.push(xs.map((x) => at.get(x) ?? null));
  }
  return rows;
}

/** The inputs left out, by reason: how many, on which axes, and the first one's time. */
export function leftOut(inputs: Inputs): { reason: Reason; count: number; axes: Axis[]; first_s: number }[] {
  const out = new Map<Reason, { reason: Reason; count: number; axes: Axis[]; first_s: number }>();
  for (const axis of AXES) {
    for (const e of inputs.axes?.[axis] ?? []) {
      if (e.counted || !e.reason) continue;
      const seen = out.get(e.reason) ?? { reason: e.reason, count: 0, axes: [], first_s: e.start_s };
      seen.count += 1;
      if (!seen.axes.includes(axis)) seen.axes.push(axis);
      seen.first_s = Math.min(seen.first_s, e.start_s);
      out.set(e.reason, seen);
    }
  }
  return REASONS.flatMap((r) => (out.has(r) ? [out.get(r)!] : []));
}

/** Counted and found inputs on each axis that has any. */
export function counts(inputs: Inputs | undefined): Partial<Record<Axis, { counted: number; found: number }>> {
  const out: Partial<Record<Axis, { counted: number; found: number }>> = {};
  for (const axis of AXES) {
    const found = inputs?.axes?.[axis] ?? [];
    if (found.length) out[axis] = { counted: found.filter((e) => e.counted).length, found: found.length };
  }
  return out;
}

/** Minutes and seconds from arming: "2:23". */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export interface GateItem {
  status: Status;
  title: string;
  detail: string;
}

const axisOf = (metric: string) => metric.slice(metric.lastIndexOf("_") + 1) as Axis;
const familyOf = (metric: string) => metric.slice("tune.".length, metric.lastIndexOf("_"));

function checkStatus(passed: boolean | null, worse?: boolean): Status {
  return passed === true ? "ok" : passed === null ? "caution" : worse ? "out" : "caution";
}

/** The gate's checklist from the AutoTune action's checks, one line per measure: the
 * attitude error per axis (the tune's measure), then the guards merged across axes.
 * `band` names a payload group; `metric` names a metric in the reader's language. */
export function gateItems(
  gate: Gate,
  reg: Registry,
  band: (group: string) => string,
  locale: Locale,
): GateItem[] {
  const tr = (key: MessageKey, vars: Record<string, string | number> = {}) => translate(locale, key, vars);
  const name = (metric: string) => reg.metrics[metric]?.name[locale] ?? metric;
  const pct = (v: number) => `${formatNumber(Math.round(v * 100), locale)}%`;
  const bandText = (b: GateBand | string) => {
    if (typeof b === "string") return band(b);
    if (b.verdict === "waiting" || b.after === undefined) return tr("tune.gate.waitingBand", { band: band(b.group) });
    return tr("tune.gate.band", {
      band: band(b.group),
      before: formatNumber(b.before, locale),
      after: formatNumber(b.after, locale),
      change: formatChange(-(b.change ?? 0), locale),
      verdict: tr(`tune.gate.verdict.${b.verdict}` as MessageKey),
    });
  };
  const items: GateItem[] = [];
  const merged = new Map<string, GateCheck[]>();
  for (const c of gate.checks) {
    if (c.check === "metric_improves" && c.metric) {
      items.push({
        status: checkStatus(c.passed, c.worse),
        title: tr("tune.gate.improves", { metric: name(c.metric), need: pct(c.by ?? 0) }),
        detail: (c.bands ?? []).map(bandText).join(" · "),
      });
      continue;
    }
    const key = c.metric ? `${c.check}|${familyOf(c.metric)}|${c.max_ratio ?? c.max_above ?? c.below ?? ""}` : c.check;
    merged.set(key, [...(merged.get(key) ?? []), c]);
  }
  for (const group of merged.values()) {
    const first = group[0]!;
    const passed = group.some((c) => c.passed === false) ? false : group.every((c) => c.passed === true) ? true : null;
    const worse = group.some((c) => c.worse);
    const axisWord = (c: GateCheck) => (c.metric ? tr(`tune.axis.${axisOf(c.metric)}` as MessageKey) : "");
    const axes = group.map(axisWord).filter(Boolean).join(", ").toLocaleLowerCase();
    // Merged across axes, each axis's bands are named with it.
    const perAxis = (c: GateCheck) => {
      const bands = (c.bands ?? []).map(bandText).join(", ");
      return bands && group.length > 1 ? `${axisWord(c)}: ${bands}` : bands;
    };
    const metric = first.metric ? name(first.metric) : "";
    let title: string;
    let detail = "";
    if (first.check === "no_shift_worse") {
      title = tr("tune.gate.noShift");
      const shifts = group.flatMap((c) => c.shifts ?? []);
      detail = shifts.length
        ? shifts.map((s) => tr("tune.gate.shift", { metric: name(s.metric), cause: s.cause ?? tr("tune.gate.noCause") })).join(" · ")
        : passed === null ? tr("tune.gate.notYet") : tr("tune.gate.noShiftNone");
    } else if (first.check === "metric_not_worse") {
      title = tr("tune.gate.notWorse", { metric: familyName(first.metric!, locale), axes, need: pct(first.by ?? 0) });
      detail = group.map(perAxis).filter(Boolean).join(" · ");
    } else if (first.check === "metric") {
      title = tr("tune.gate.below", { metric: familyName(first.metric!, locale), axes, value: formatNumber(first.below ?? first.above ?? 0, locale) });
      detail = passed === null ? tr("tune.gate.notMeasured") : "";
    } else if (first.check === "metric_vs_lightest") {
      title = first.max_ratio !== undefined
        ? tr("tune.gate.ratio", { metric: familyName(first.metric!, locale), axes, ratio: formatNumber(first.max_ratio, locale) })
        : tr("tune.gate.above", { metric: familyName(first.metric!, locale), axes, above: formatNumber(first.max_above ?? 0, locale) });
      detail = group
        .filter((c) => c.light !== undefined && c.heavy !== undefined)
        .map((c) => `${axisWord(c)}: ${tr("tune.gate.lightHeavy", { light: formatNumber(c.light!, locale), heavy: formatNumber(c.heavy!, locale) })}`)
        .join(" · ") || (passed === null ? tr("tune.gate.needsBoth") : "");
    } else {
      title = metric || first.check;
    }
    items.push({ status: checkStatus(passed, worse), title: sentence(title), detail });
  }
  if (gate.changes.length) {
    const done = gate.changes.every((c) => c.status === "confirmed");
    items.push({
      status: done ? "ok" : gate.changes.some((c) => c.status === "regressed") ? "out" : "caution",
      title: tr("tune.gate.changes"),
      detail: gate.changes.map((c) => `${c.param} ${formatNumber(c.value, locale)}: ${tr(`tune.gate.status.${c.status.replace(/ /g, "_")}` as MessageKey)}`).join(" · "),
    });
  }
  return items;
}

/** A metric family's name without its axis: "rate tracking error". */
export function familyName(metric: string, locale: Locale): string {
  return translate(locale, `tune.family.${familyOf(metric)}` as MessageKey);
}

const sentence = (s: string) => s.charAt(0).toLocaleUpperCase() + s.slice(1);
