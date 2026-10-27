// An airframe's parameter history (spec: Parameter history and change attribution;
// User interface, "Parameter history"; mock-up History): every change set with its
// origin and its measured effect, in-flight A/B splits, revert tests and credits, newest
// first, and the ranked changes behind each shift. The engine's trend.events,
// trend.attribution, trend.revert_test, trend.credits, trend.fleet and flight.ab give
// every verdict and number; the helpers here only group, order and word them.

import { translate, type Locale, type MessageKey } from "./i18n";
import { formatNumber, unitText, type FlightRow, type Text } from "./report";
import type { Status } from "./status";
import type { TrendEvent } from "./trends";

export type Verdict = "worse" | "better" | "moved" | "inconclusive";
export type Confidence = "high" | "medium" | "low";

/** A parameter's value before and after a change, in canonical names and units. */
export interface ParamChange {
  name: string;
  before: number | string | null;
  after: number | string | null;
  class: string | null;
}

/** A change a shift brackets: a parameter, a build or a firmware change, from the flight it first applies to. */
export interface ShiftChange extends ParamChange {
  kind: "parameter" | "build" | "firmware";
  flight: string;
  origin: string | null;
}

/** How the map ranked a change: its score, the link that ties it to the metric
 * ("fleet": learned from the platform), the link's reason, and each factor. */
export interface Ranked {
  score: number;
  link: string | null;
  why: Text | null;
  factors: Record<string, number>;
}

export interface ShiftCandidate extends ShiftChange, Ranked {
  fleet: { agree: number; against: number } | null;
}

export interface Weather {
  values: Record<string, [number, number]>; // median before, median after
  differs: string[];
}

/** A shift as trend.attribution gives it (history/attribution.py, shift_json). */
export interface Shift {
  metric: string;
  name: Text;
  unit: string;
  segment: string;
  group: string;
  flight: string; // the first flight after
  before: string[];
  after: string[];
  effect: number;
  low: number;
  high: number;
  spread: number | null; // null: a pair of flights compared by their intervals
  verdict: Verdict;
  confidence: Confidence;
  candidates: ShiftCandidate[];
  context: ShiftChange[];
  weather: Weather | null;
}

export interface SplitChange extends ParamChange {
  origin: string | null; // "in flight" (a ground station's write) or "tuning knob"
}

export interface SplitResult {
  metric: string;
  name: Text;
  unit: string;
  segment: string;
  method: "intervals" | "blocks";
  before: number;
  after: number;
  effect: number;
  low: number;
  high: number;
  verdict: Verdict;
  confidence: Confidence;
  candidates: (SplitChange & Ranked)[];
}

/** An in-flight A/B split as flight.ab gives it (history/ab.py, split_json), in seconds after arming. */
export interface Split {
  at_s: number;
  changes: SplitChange[];
  context: SplitChange[];
  before_s: [number, number] | null;
  after_s: [number, number] | null;
  skipped: string | null;
  results: SplitResult[];
}

export type StepState = "to fly" | "explained" | "not this one" | "unclear" | "not needed";

export interface RevertStep extends ParamChange {
  kind: string;
  order: number;
  state: StepState;
  score: number;
  tested_on: string | null;
  value: number | null;
}

/** A shift's revert test as trend.revert_test gives it (history/revert.py, revert_json). */
export interface RevertTest {
  metric: string;
  name: Text;
  unit: string;
  segment: string;
  group: string;
  flight: string;
  before: number;
  after: number;
  band: number | null;
  explained_by: string | null;
  next_step: number | null;
  steps: RevertStep[];
  by_hand: (ParamChange & { kind: string })[];
}

/** A credit as trend.credits gives it (history/credit.py, credit_json). */
export interface Credit {
  metric: string;
  name: Text;
  unit: string;
  segment: string;
  group: string;
  flight: string;
  effect: number;
  confidence: Confidence;
  param: string;
  before: number | null;
  after: number | null;
  link: string | null;
  kept: boolean;
  undone_on: string | null;
  value_then: number | null;
}

/** A link the platform's airframes confirmed (trend.fleet). */
export interface FleetLink {
  param: string;
  changed: "up" | "down";
  metric: string;
  moved: "up" | "down";
  airframes: number;
  against: number;
  in_map: boolean;
  link: string | null;
}

/** A stored recommendation (recommend.history), read for the AutoTune action. */
export interface RecRow {
  id: string;
  finding_code: string | null;
  param: string | null;
  detail: { applied_flight?: string | null };
}

/** The finding whose action is AutoTune (knowledge/procedures/copter.json). */
export const AUTOTUNE = "tune.autotune_offer";
/** How many worse shifts get their revert test read, newest first. */
export const MAX_REVERTS = 20;
/** How many of the newest flights are asked for in-flight changes. */
export const MAX_AB = 30;
/** flight.list's cap: with that many, the oldest flights are not all listed. */
export const LIST_CAP = 200;

// Tuning and calibration changes are changes; learned values, counters, hardware IDs and
// ground-station settings change on their own and are set aside (spec: "Parameters that
// change on their own").
const CHANGE_CLASSES = new Set(["tuning", "calibration"]);
export const isChange = (c: { class: string | null }) => c.class === null || CHANGE_CLASSES.has(c.class);

export interface ChangeSet {
  origin: string;
  changes: ParamChange[];
  aside: ParamChange[];
}

export interface Grade {
  id: string;
  param: string | null;
  proposed: number | null;
  grade: string;
  autotune: boolean;
  doneOn: string | null; // the AutoTune flight
}

export interface Reading {
  test: RevertTest;
  step: RevertStep;
}

/** One entry of the history: what first applied at a flight's start, or an in-flight split. */
export interface Row {
  key: string;
  flight: string;
  phase: "start" | "in flight";
  order: number; // the flight's place in flying order, 0 the oldest
  sets: ChangeSet[];
  builds: { number: number; reason: string | null }[];
  firmware: { from: string; to: string }[];
  grades: Grade[];
  afterAutotune: boolean;
  shifts: Shift[];
  readings: Reading[]; // revert-test steps this flight flew
  credits: Credit[]; // credits of this entry's shifts
  undoes: Credit[]; // credited changes this flight put back
  split: Split | null;
}

export interface HistoryData {
  flights: FlightRow[]; // newest first, as flight.list gives them
  events: TrendEvent[];
  shifts: Shift[];
  credits: Credit[];
  reverts: RevertTest[];
  splits: Record<string, Split[]>;
  recs: RecRow[];
}

export const shiftKey = (s: { metric: string; segment: string; group: string; flight: string }) =>
  `${s.metric}|${s.segment}|${s.group}|${s.flight}`;

/** The flight where a shift's own changes first applied: the boundary its candidates
 * at full timing sit at. Its first flight after can be later, when the boundary flight
 * did not measure the metric. */
export function boundary(s: Shift): string {
  return s.candidates.find((c) => c.factors.timing === 1)?.flight ?? s.flight;
}

/** Each flight's number in the airframe's flying order, the oldest 1; none when the
 * list is cut at its cap, since the oldest are then missing. */
export function flightNumbers(flights: FlightRow[]): Map<string, number> {
  if (flights.length >= LIST_CAP) return new Map();
  return new Map(flights.map((f, i) => [f.id, flights.length - i]));
}

/** The history's entries, newest first. */
export function buildRows(d: HistoryData): Row[] {
  const order = new Map(d.flights.map((f, i) => [f.id, d.flights.length - 1 - i]));
  const rows = new Map<string, Row>();
  const row = (flight: string, phase: Row["phase"] = "start", at = 0): Row | null => {
    const o = order.get(flight);
    if (o === undefined) return null;
    const key = phase === "start" ? `${flight}|start` : `${flight}|ab|${at}`;
    let r = rows.get(key);
    if (!r) {
      r = {
        key, flight, phase, order: o, sets: [], builds: [], firmware: [], grades: [], afterAutotune: false,
        shifts: [], readings: [], credits: [], undoes: [], split: null,
      };
      rows.set(key, r);
    }
    return r;
  };
  const recs = new Map(d.recs.map((r) => [r.id, r]));
  for (const e of d.events) {
    if (e.flight === null) continue;
    const r = row(e.flight);
    if (!r) continue;
    switch (e.kind) {
      case "parameter change":
      case "build change":
      case "ground station change": {
        const all: ParamChange[] = e.changes ?? e.params.map((name) => ({ name, before: null, after: null, class: null }));
        r.sets.push({ origin: e.origin, changes: all.filter(isChange), aside: all.filter((c) => !isChange(c)) });
        break;
      }
      case "build":
        r.builds.push({ number: e.number, reason: e.reason });
        break;
      case "firmware":
        r.firmware.push({ from: e.from, to: e.to });
        break;
      case "recommendation": {
        const rec = recs.get(e.id);
        const autotune = rec?.finding_code === AUTOTUNE;
        r.grades.push({ id: e.id, param: e.param, proposed: e.proposed, grade: e.grade, autotune, doneOn: autotune ? (rec?.detail.applied_flight ?? null) : null });
        break;
      }
    }
  }
  // AutoTune saves its gains as it lands: they first apply to the flight after it.
  const flying = [...d.flights].reverse();
  for (const rec of d.recs) {
    const done = rec.finding_code === AUTOTUNE ? rec.detail.applied_flight : null;
    const at = done ? order.get(done) : undefined;
    const next = at === undefined ? undefined : flying[at + 1];
    const r = next ? rows.get(`${next.id}|start`) : undefined;
    if (r && r.sets.some((s) => s.changes.length)) r.afterAutotune = true;
  }
  for (const s of d.shifts) row(boundary(s))?.shifts.push(s);
  for (const test of d.reverts) {
    for (const step of test.steps) if (step.tested_on) row(step.tested_on)?.readings.push({ test, step });
  }
  const byShift = new Map<string, Row>();
  for (const r of rows.values()) for (const s of r.shifts) byShift.set(shiftKey(s), r);
  for (const c of d.credits) {
    (byShift.get(shiftKey(c)) ?? row(c.flight))?.credits.push(c);
    if (c.undone_on) row(c.undone_on)?.undoes.push(c);
  }
  for (const [flight, splits] of Object.entries(d.splits)) {
    for (const s of splits) {
      const r = row(flight, "in flight", s.at_s);
      if (r) r.split = s;
    }
  }
  for (const r of rows.values()) r.shifts.sort((a, b) => VERDICTS.indexOf(a.verdict) - VERDICTS.indexOf(b.verdict));
  return [...rows.values()]
    .filter(hasContent)
    .sort((a, b) =>
      b.order - a.order
      || (a.phase === b.phase ? 0 : a.phase === "in flight" ? -1 : 1) // during a flight comes after its start
      || (b.split?.at_s ?? 0) - (a.split?.at_s ?? 0),
    );
}

const VERDICTS: Verdict[] = ["worse", "better", "moved", "inconclusive"];

const hasContent = (r: Row) =>
  r.sets.some((s) => s.changes.length) || r.builds.length > 0 || r.firmware.length > 0 || r.grades.length > 0
  || r.shifts.length > 0 || r.readings.length > 0 || r.credits.length > 0 || r.undoes.length > 0 || r.split !== null;

export type Filter = "all" | "params" | "build" | "firmware" | "autotune" | "effect";
export const FILTERS: Filter[] = ["all", "params", "build", "firmware", "autotune", "effect"];

/** Whether an entry belongs under a filter chip. A measured effect is a shift, an A/B
 * result or a revert test that said something: not inconclusive, not unclear. */
export function matches(r: Row, f: Filter): boolean {
  switch (f) {
    case "params":
      return r.split !== null || r.sets.some((s) => s.changes.length) || r.grades.some((g) => g.param !== null);
    case "build":
      return r.builds.length > 0;
    case "firmware":
      return r.firmware.length > 0;
    case "autotune":
      return r.afterAutotune || r.grades.some((g) => g.autotune);
    case "effect":
      return r.shifts.some((s) => s.verdict !== "inconclusive")
        || (r.split?.results.some((x) => x.verdict !== "inconclusive") ?? false)
        || r.readings.some((x) => x.step.state === "explained" || x.step.state === "not this one");
    default:
      return true;
  }
}

/** The tuning and calibration changes of an entry, every change set together. */
export const changesOf = (r: Row): ParamChange[] => r.sets.flatMap((s) => s.changes);

/** What an entry changed, item by item: each parameter (mono, with a note for the
 * tuning knob or a calibration), then builds and firmware in words. A single build or
 * firmware change is the entry's title already, so it has no list. */
export function changeItems(r: Row, locale: Locale): { text: string; mono: boolean; note: string | null }[] {
  if (r.split) {
    return r.split.changes.map((c) => ({
      text: changeText(c, locale),
      mono: true,
      note: c.origin === "tuning knob" ? translate(locale, "history.origin.tuning_knob") : null,
    }));
  }
  const params = changesOf(r);
  if (!params.length && r.builds.length + r.firmware.length === 1) return [];
  return [
    ...params.map((c) => ({ text: changeText(c, locale), mono: true, note: c.class === "calibration" ? classText(c.class, locale) : null })),
    ...r.builds.map((b) => ({
      text: b.reason ? translate(locale, "history.buildReason", { number: b.number, reason: b.reason }) : translate(locale, "history.build", { number: b.number }),
      mono: false,
      note: null,
    })),
    ...r.firmware.map((f) => ({ text: translate(locale, "trends.event.firmware", { from: f.from, to: f.to }), mono: false, note: null })),
  ];
}

/** An entry's title: the A/B's leading change, the revert test, the one change, or how many at once. */
export function rowTitle(r: Row, locale: Locale): string {
  const tr = (key: MessageKey, vars: Record<string, string | number> = {}) => translate(locale, key, vars);
  if (r.split) return tr("history.title.ab", { params: leadNames(r.split, locale) });
  if (r.readings.length) {
    return tr("history.title.revert", { params: [...new Set(r.readings.map((x) => x.step.name))].join(", ") });
  }
  const params = changesOf(r);
  const count = params.length + r.builds.length + r.firmware.length;
  if (count === 0) {
    if (r.grades.some((g) => g.autotune)) return tr("history.title.autotuneGraded");
    if (r.grades.length) return tr("history.title.graded");
    return tr("history.title.effect");
  }
  if (count === 1 && r.firmware[0]) return tr("trends.event.firmware", { from: r.firmware[0].from, to: r.firmware[0].to });
  const build = r.builds[0];
  if (count === 1 && build) {
    return build.reason ? tr("history.buildReason", { number: build.number, reason: build.reason }) : tr("history.build", { number: build.number });
  }
  if (count === 1 && params[0]) return params[0].name;
  return tr("history.title.many", { count });
}

/** The changes an A/B split is named after: the top-ranked change of each result, or
 * every change it made. */
function leadNames(s: Split, locale: Locale): string {
  const top = s.results.flatMap((x) => (x.candidates[0] ? [x.candidates[0].name] : []));
  const names = [...new Set(top.length ? top : s.changes.map((c) => c.name))];
  if (names.length <= 2) return names.join(", ");
  return translate(locale, "history.andMore", { first: names.slice(0, 2).join(", "), count: names.length - 2 });
}

/** Where the entry's changes came from, in the reader's language. */
export function origins(r: Row, locale: Locale): string[] {
  const out = new Set<string>();
  for (const s of r.sets) if (s.changes.length) out.add(originText(s.origin, locale));
  for (const c of r.split?.changes ?? []) {
    // In a split, a change is the tuning knob's or a ground station's write (PARM).
    if (c.origin) out.add(translate(locale, c.origin === "tuning knob" ? "history.origin.tuning_knob" : "history.origin.gcs_in_flight"));
  }
  for (const b of r.builds) out.add(translate(locale, "history.build", { number: b.number }));
  if (r.firmware.length) out.add(translate(locale, "history.origin.firmware"));
  return [...out];
}

const ORIGINS: Record<string, MessageKey> = {
  "in flight": "history.origin.in_flight",
  "loaded file": "history.origin.loaded_file",
  "ground station edit": "history.origin.ground_station_edit",
  unexplained: "history.origin.unexplained",
  "tuning knob": "history.origin.tuning_knob",
};

export function originText(origin: string, locale: Locale): string {
  const key = ORIGINS[origin];
  return key ? translate(locale, key) : origin;
}

const CLASSES: Record<string, MessageKey> = {
  tuning: "history.class.tuning",
  calibration: "history.class.calibration",
  learned: "history.class.learned",
  counter: "history.class.counter",
  hardware: "history.class.hardware",
  "ground station": "history.class.ground_station",
};

export function classText(cls: string | null, locale: Locale): string {
  const key = cls ? CLASSES[cls] : undefined;
  return key ? translate(locale, key) : (cls ?? "");
}

/** Set-aside changes by class: "learned in flight: MOT_THST_HOVER · counter: STAT_BOOTCNT". */
export function asideText(aside: ParamChange[], locale: Locale): string {
  const by = new Map<string, string[]>();
  for (const c of aside) by.set(c.class ?? "", [...(by.get(c.class ?? "") ?? []), c.name]);
  return [...by.entries()].map(([cls, names]) => `${classText(cls, locale)}: ${names.join(", ")}`).join(" · ");
}

/** A parameter's value as people set it: up to six significant digits, no float noise
 * (0.13500000536441803 reads 0.135). Missing reads "–". */
export function paramValue(v: number | string | null | undefined, locale: Locale): string {
  if (v === null || v === undefined) return "–";
  if (typeof v === "string") return v;
  if (!Number.isFinite(v)) return "–";
  return new Intl.NumberFormat(locale === "uk" ? "uk-UA" : "en-GB", {
    maximumSignificantDigits: 6,
    useGrouping: false,
  }).format(v);
}

/** "INS_HNTCH_BW 40 → 20". */
export const changeText = (c: ParamChange, locale: Locale) =>
  `${c.name} ${paramValue(c.before, locale)} → ${paramValue(c.after, locale)}`;

/** A signed number: "+5.25", "−1.6", "±0". */
export function signed(v: number, locale: Locale): string {
  const sign = v < 0 ? "−" : v > 0 ? "+" : "±";
  return `${sign}${formatNumber(Math.abs(v), locale)}`;
}

/** A difference in the metric's unit; a percentage metric moves in percentage points. */
export function effectText(effect: number, unit: string, locale: Locale): string {
  const n = signed(effect, locale);
  if (unit === "%") return translate(locale, "history.pp", { value: n });
  if (!unit || unit === "count" || unit === "ratio") return n;
  return `${n} ${unitText(unit, locale)}`;
}

/** The 95% interval of a difference: "+4.25 to +6.25". */
export const intervalText = (low: number, high: number, locale: Locale) =>
  translate(locale, "history.interval", { low: signed(low, locale), high: signed(high, locale) });

/** How a measured difference reads: its status and words. `how` is what it was measured
 * by: flights against this airframe's spread, a pair of flights' intervals, or an A/B's
 * sides by their intervals or by blocks. */
export function verdictLook(
  verdict: Verdict,
  how: "spread" | "pair" | "intervals" | "blocks",
  locale: Locale,
): { status: Status; text: string } {
  const apart = how === "pair" || how === "intervals";
  if (verdict === "inconclusive") {
    return { status: "info", text: translate(locale, apart ? "history.verdict.overlap" : "history.verdict.zero") };
  }
  const status: Status = verdict === "worse" ? "out" : verdict === "better" ? "ok" : "info";
  const by: MessageKey = apart ? "history.by.apart" : how === "blocks" ? "history.by.blocks" : "history.by.spread";
  return {
    status,
    text: translate(locale, "history.verdict", { verdict: translate(locale, `history.verdict.${verdict}` as MessageKey), by: translate(locale, by) }),
  };
}

export const shiftHow = (s: Shift) => (s.spread === null ? "pair" : "spread");

/** The ranking's factors as the engine multiplied them: "map 0.6 × direction 0.6 × …". */
export function factorsText(factors: Record<string, number>, locale: Locale): string {
  return FACTORS.filter((k) => k in factors)
    .map((k) => `${translate(locale, `history.factor.${k}` as MessageKey)} ${formatNumber(factors[k] ?? 0, locale)}`)
    .join(" × ");
}

const FACTORS = ["strength", "direction", "size", "timing", "fleet"];

/** A candidate's bar: its score against the larger of 1 and the top score. */
export function scoreShare(score: number, scores: number[]): number {
  const top = Math.max(1, ...scores);
  return Math.min(1, Math.max(0, score / top));
}

/** A candidate's name: the parameter, the build or the firmware update. */
export function candidateName(c: { kind?: string; name: string; before: number | string | null; after: number | string | null }, locale: Locale): string {
  if (c.kind === "build") {
    const n = /^build (\d+)$/.exec(c.name)?.[1];
    return n ? translate(locale, "history.build", { number: n }) : c.name;
  }
  if (c.kind === "firmware") return translate(locale, "trends.event.firmware", { from: String(c.before ?? "–"), to: String(c.after ?? "–") });
  return c.name;
}

/** Why the map ranked a candidate as it did, in the reader's language. */
export function candidateWhy(c: Ranked & { kind?: string; after: number | string | null }, metric: string, locale: Locale): string {
  if (c.why) return c.why[locale];
  if (c.link === "fleet") return translate(locale, "history.why.fleet");
  if (c.kind === "build") return typeof c.after === "string" && c.after ? c.after : translate(locale, "history.why.build");
  if (c.kind === "firmware") return translate(locale, "history.why.firmware");
  return translate(locale, "history.why.unlinked", { metric });
}

/** The platform's confirmed link for this candidate, the way it was changed and the
 * way the metric moved, if trend.fleet lists one. */
export function fleetLink(
  links: FleetLink[],
  c: { kind?: string; name: string; before: number | string | null; after: number | string | null },
  metric: string,
  effect: number,
): FleetLink | null {
  if ((c.kind ?? "parameter") !== "parameter" || typeof c.before !== "number" || typeof c.after !== "number" || c.before === c.after) return null;
  const changed = c.after > c.before ? "up" : "down";
  const moved = effect > 0 ? "up" : "down";
  return links.find((l) => l.param === c.name && l.metric === metric && l.changed === changed && l.moved === moved) ?? null;
}

const WEATHER: Record<string, { key: MessageKey; unit: MessageKey }> = {
  wind_ms: { key: "history.weather.wind_ms", unit: "history.unit.ms" },
  gust_max_ms: { key: "history.weather.gust_max_ms", unit: "history.unit.ms" },
  density_altitude_m: { key: "history.weather.density_altitude_m", unit: "history.unit.m" },
  temperature_c: { key: "history.weather.temperature_c", unit: "history.unit.degC" },
};

/** The weather either side of a shift: each variable's medians and whether they differ
 * beyond the engine's threshold. */
export function weatherRows(w: Weather | null, locale: Locale): { name: string; before: string; after: string; differs: boolean }[] {
  if (!w) return [];
  return Object.entries(w.values).map(([k, [b, a]]) => {
    const v = WEATHER[k];
    const unit = v ? ` ${translate(locale, v.unit)}` : "";
    return {
      name: v ? translate(locale, v.key) : k,
      before: `${formatNumber(b, locale)}${unit}`,
      after: `${formatNumber(a, locale)}${unit}`,
      differs: w.differs.includes(k),
    };
  });
}

/** Whether a build or firmware change sat in the same window as the top candidate:
 * then the ranking cannot be high confidence. */
export const hardwareWith = (s: Shift) => s.candidates.slice(1).filter((c) => c.kind === "build" || c.kind === "firmware");

/** The status of a revert test's step. */
export function stepStatus(state: StepState): Status {
  return state === "explained" ? "ok" : state === "to fly" ? "caution" : "info";
}

/** A flight's date, without the time: "24 Oct 2026". */
export function flightDate(utcUs: number | null, locale: Locale): string | null {
  if (utcUs === null) return null;
  return new Intl.DateTimeFormat(locale === "uk" ? "uk-UA" : "en-GB", { dateStyle: "medium" }).format(new Date(utcUs / 1000));
}

/** A small chart of the metric around a shift, in a w×h box: one dot per flight, the
 * airframe's baseline as a band, and the x of any flight. */
export function miniChart(
  points: { flight: string; value: number }[],
  baseline: { low: number; high: number } | null,
  w: number,
  h: number,
): { dots: { x: number; y: number; flight: string }[]; path: string; band: { y: number; h: number } | null; x: (flight: string) => number | null } {
  const values = points.map((p) => p.value);
  if (baseline) values.push(baseline.low, baseline.high);
  let lo = values.length ? Math.min(...values) : 0;
  let hi = values.length ? Math.max(...values) : 1;
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.15;
  lo -= pad;
  hi += pad;
  const top = 6;
  const y = (v: number) => top + (h - top) * (1 - (v - lo) / (hi - lo));
  const step = points.length > 1 ? (w - 16) / (points.length - 1) : 0;
  const xi = (i: number) => (points.length > 1 ? 8 + i * step : w / 2);
  const dots = points.map((p, i) => ({ x: xi(i), y: y(p.value), flight: p.flight }));
  const at = new Map(points.map((p, i) => [p.flight, i]));
  return {
    dots,
    path: dots.map((d, i) => `${i ? "L" : "M"}${d.x.toFixed(1)} ${d.y.toFixed(1)}`).join(" "),
    band: baseline ? { y: y(baseline.high), h: Math.max(1, y(baseline.low) - y(baseline.high)) } : null,
    x: (flight) => {
      const i = at.get(flight);
      return i === undefined ? null : xi(i);
    },
  };
}
