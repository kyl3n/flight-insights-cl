// The Navigation and GNSS tab of one flight, as the engine's flight.navigation returns it,
// and the pure helpers behind it (spec: User interface, "The Navigation and GNSS view";
// mock-up Navigation): GNSS events, outages and drills, how the aircraft coped with each,
// the evidence each receiver gave, the readiness to fly without GNSS, and what would get
// it home. Every status here is the engine's; these helpers only word and place it.
// Times are seconds after arming, distances relative metres from the analysis.

import { CATALOGS, translate, type Locale, type MessageKey } from "./i18n";
import type { ProcedureStep } from "./plan";
import { formatNumber, unitText, type Severity, type Text } from "./report";
import type { Status } from "./status";

export type EventClass = "jamming" | "spoofing" | "obstruction" | "onboard" | "receiver";
export type RowId =
  | "estimator" | "source" | "hold" | "drift" | "resets" | "failsafe" | "mode" | "heading"
  | "resumed";
export type Area =
  | "position" | "switching" | "heading" | "height" | "wind" | "failsafes" | "pilot_fallback";
export type AreaStatus = "ready" | "gap" | "none" | "unavailable";
export type Channel = "ublox_status" | "fix" | "accuracy" | "gps_time";

/** A finding as the view gives it: the registry words it from its code and params. */
export interface FindingRef {
  id: string;
  code: string;
  severity: Severity;
  confidence: "low" | "medium" | "high";
  params: Record<string, unknown>;
}

export interface Judged {
  finding: FindingRef;
  status: Status;
}

/** One line of how the aircraft coped, with the engine's status and the verdict that
 * says which wording applies. */
export interface CopingRow {
  id: RowId;
  status: Status;
  verdict: string;
  values: Record<string, unknown>;
}

export interface NavEvent {
  n: number;
  class: EventClass;
  confidence: "low" | "medium" | "high";
  start_s: number;
  end_s: number;
  duration_s: number;
  receivers: number[]; // GPS 1, GPS 2; none for the estimator's own checks
  channels: string[];
  peaks: Record<string, number>;
  position: Record<string, number> | null; // the largest gaps and resets, whole metres
  altitude_gap_m: number | null;
  time: { longest_time_stop_s: number; largest_time_jump_s: number } | null;
  finding: FindingRef | null;
  heading_finding: FindingRef | null;
  outages: number[];
  coping: CopingRow[];
  evidence: string[];
}

export interface Outage {
  n: number;
  drill: boolean;
  start_s: number;
  end_s: number;
  duration_s: number;
  using_gnss_at_start: boolean;
  detection_s: number | null;
  absolute_s: number;
  kept_s: number;
  held: boolean;
  kept_relative: boolean;
  resumed_s: number | null;
  source_set: number | null;
  failsafes: string[];
  mode: string | null;
  mode_reason: string | null;
  drift: { drift_m: number; drift_m_per_min?: number; minutes_to_tolerance?: number } | null;
  finding: FindingRef | null;
  events: number[];
  coping: CopingRow[];
  evidence: string[];
}

export interface Receiver {
  instance: number;
  param: string; // GPS1_TYPE
  type: string | null; // its value's name, as ArduPilot gives it
  logged: Record<Channel, boolean>;
}

export interface AreaRow {
  area: Area;
  status: AreaStatus;
  say: string; // which of the readiness analyzer's sentences applies
  fill: Record<string, unknown>;
  values: Record<string, unknown>;
  evidence: string;
}

export interface Action {
  finding: FindingRef;
  steps: { kind: ProcedureStep["kind"]; text: Text }[];
}

export interface NavigationView {
  flight: string;
  airframe_id: string | null;
  current: boolean;
  skipped: Record<"gnss" | "no_gnss" | "no_gnss_readiness", string | null>;
  events: NavEvent[];
  outages: Outage[];
  receivers: Receiver[];
  independent: { estimator: boolean; barometer: boolean; second_receiver: boolean };
  readiness: AreaRow[];
  actions: Action[];
  sources: string[];
}

export const CLASSES: EventClass[] = ["jamming", "spoofing", "obstruction", "onboard", "receiver"];
export const CHANNELS: Channel[] = ["ublox_status", "fix", "accuracy", "gps_time"];

/** What "How the aircraft coped" shows: an event or an outage. */
export interface Pick {
  kind: "event" | "outage";
  n: number;
}

/** The first event, else the first outage or drill, else nothing. */
export function firstPick(v: NavigationView): Pick | null {
  const first = v.events[0] ?? v.outages[0];
  if (!first) return null;
  return { kind: v.events.length ? "event" : "outage", n: first.n };
}

export function picked(v: NavigationView, p: Pick | null): NavEvent | Outage | null {
  if (!p) return null;
  return (p.kind === "event" ? v.events : v.outages).find((x) => x.n === p.n) ?? null;
}

const num = (v: unknown, locale: Locale) => (typeof v === "number" ? formatNumber(v, locale) : "–");

/** A stretch in seconds after arming: "23.5–59.2 s". */
export function span(start: number, end: number, locale: Locale): string {
  return `${formatNumber(start, locale)}–${formatNumber(end, locale)} ${unitText("s", locale)}`;
}

/** "GPS 1", "GPS 1 and GPS 2"; with no receiver named, the estimator's own checks. */
export function receiversLabel(receivers: number[], locale: Locale): string {
  if (!receivers.length) return translate(locale, "nav.receivers.none");
  const names = receivers.map((r) => `GPS ${r}`);
  const last = names.pop() ?? "";
  return names.length ? translate(locale, "nav.receivers.both", { a: names.join(", "), b: last }) : last;
}

/** The readiness review's statuses in the status language. */
export const AREA_LOOK: Record<AreaStatus, { status: Status; label: MessageKey }> = {
  ready: { status: "ok", label: "nav.areaStatus.ready" },
  gap: { status: "caution", label: "nav.areaStatus.gap" },
  none: { status: "out", label: "nav.areaStatus.none" },
  unavailable: { status: "info", label: "nav.areaStatus.unavailable" },
};

function has(key: string): key is MessageKey {
  return key in CATALOGS.en;
}

function list(v: unknown, locale: Locale): string {
  return Array.isArray(v) ? v.map((x) => (typeof x === "number" ? formatNumber(x, locale) : String(x))).join(", ") : "";
}

function sensors(v: unknown, locale: Locale): string {
  if (!Array.isArray(v)) return "";
  return v
    .map((s) => {
      const key = `nav.sensor.${String(s)}`;
      return has(key) ? translate(locale, key) : String(s);
    })
    .join(", ");
}

/** What the area is now, as the readiness analyzer says it, in the reader's language. */
export function areaText(row: AreaRow, locale: Locale): string {
  const v = row.values;
  const key = `nav.area.${row.area}.${row.say}`;
  if (!has(key)) return row.say;
  // the sets that keep a position, those naming sensors not set up, or those losing the heading
  const sets = row.fill.sets ?? (row.status === "ready" ? v.usable_sets : v.sets);
  const parts = [
    translate(locale, key, {
      sets: list(sets, locale),
      sensors: sensors(v.sensors, locale),
      channel: num(row.fill.channel, locale),
      compasses: num(v.compasses_in_use, locale),
      modes: list(v.modes, locale),
    }),
  ];
  if (row.area === "height" && v.rangefinder_down) parts.push(translate(locale, "nav.area.height.rangefinder"));
  if (row.area === "wind" && v.baro_wind_compensation) parts.push(translate(locale, "nav.area.wind.baro"));
  if (row.area === "failsafes" && row.say === "ekf") {
    const action = `nav.area.failsafes.action.${String(v.FS_EKF_ACTION ?? 1)}`;
    parts[0] = translate(locale, key, { action: has(action) ? translate(locale, action) : String(v.FS_EKF_ACTION) });
    parts.push(translate(locale, v.dead_reckoning_failsafe_acts ? "nav.area.failsafes.dr" : "nav.area.failsafes.noDr"));
  }
  if (row.area === "pilot_fallback") {
    const drill = Array.isArray(v.drill_switch_channels) ? (v.drill_switch_channels as number[])[0] : undefined;
    parts.push(
      drill !== undefined
        ? translate(locale, "nav.area.pilot_fallback.drill", { channel: drill })
        : translate(locale, "nav.area.pilot_fallback.noDrill"),
    );
  }
  return parts.join("; ");
}

/** What happened, for one row of "How the aircraft coped". The failsafes the engine
 * judged are listed by their findings' own titles instead (empty here). */
export function copingText(row: CopingRow, locale: Locale): string {
  const v = row.values;
  if (row.id === "failsafe" && row.verdict === "judged") return "";
  const key = `nav.cope.${row.id}.${row.verdict}`;
  if (!has(key)) return row.verdict;
  const vars: Record<string, string | number> = {};
  for (const [k, x] of Object.entries(v)) {
    vars[k] = k === "sensors" ? sensors(x, locale) : Array.isArray(x) ? list(x, locale) : num(x, locale);
    if (typeof x === "string") vars[k] = x;
  }
  let text = translate(locale, key, vars);
  if (row.id === "estimator" && row.verdict === "rejected" && typeof v.ratio === "number") {
    text += translate(locale, "nav.cope.estimator.ratio", { ratio: num(v.ratio, locale) });
  }
  if (row.id === "estimator" && row.verdict === "followed" && typeof v.innovation === "number") {
    text += translate(locale, "nav.cope.estimator.innovation", { innovation: num(v.innovation, locale) });
  }
  if (row.id === "estimator" && row.verdict === "not_using" && typeof v.set === "number") {
    text += translate(locale, "nav.cope.estimator.onSet", { set: v.set });
  }
  if (row.id === "drift" && typeof v.rate === "number") {
    text += translate(locale, "nav.cope.drift.rate", { rate: num(v.rate, locale), minutes: num(v.minutes, locale) });
  }
  if (row.id === "heading" && v.no_reset === true) {
    text = `${translate(locale, "nav.cope.heading.noReset")}; ${text}`;
  }
  return text;
}

/** The short word beside a row's status: the verdict's own where it has one. */
export function verdictWord(row: CopingRow, locale: Locale): string {
  const own = row.verdict === "judged" ? `nav.verdict.failsafe.${row.status}` : `nav.verdict.${row.id}.${row.verdict}`;
  return has(own) ? translate(locale, own) : translate(locale, `status.${row.status}` as MessageKey);
}

export function rowLabel(row: CopingRow, locale: Locale): string {
  return translate(locale, `nav.row.${row.id}` as MessageKey);
}

/** An event's worst readings and largest relative distances, as the analysis stored them. */
export function eventFacts(e: NavEvent, locale: Locale): string[] {
  const out: string[] = [];
  for (const [k, x] of Object.entries(e.peaks)) {
    const key = `nav.peak.${k}`;
    if (has(key)) out.push(translate(locale, key, { v: k === "lowest_agc_ratio" ? formatNumber(x * 100, locale) : formatNumber(x, locale) }));
  }
  const p = e.position;
  if (p?.largest_track_gap_m !== undefined) out.push(translate(locale, "nav.peak.track_gap", { m: formatNumber(p.largest_track_gap_m, locale) }));
  if (p?.largest_receivers_gap_m !== undefined) out.push(translate(locale, "nav.peak.receivers_gap", { m: formatNumber(p.largest_receivers_gap_m, locale) }));
  if (e.altitude_gap_m !== null) out.push(translate(locale, "nav.peak.altitude_gap", { m: formatNumber(e.altitude_gap_m, locale) }));
  if (e.time) {
    out.push(translate(locale, "nav.peak.time", {
      stop: formatNumber(e.time.longest_time_stop_s, locale),
      jump: formatNumber(e.time.largest_time_jump_s, locale),
    }));
  }
  return out;
}

/** The goal's status: met, or not. */
export function goalLook(met: boolean): { status: Status; label: MessageKey } {
  return met ? { status: "ok", label: "nav.goal.met" } : { status: "out", label: "nav.goal.notMet" };
}

/** A procedure's own steps, as the shared step list shows them. */
export function actionSteps(a: Action): ProcedureStep[] {
  return a.steps.map((s) => ({ kind: s.kind, text: s.text, params: [], step: null, checks: [] }));
}
