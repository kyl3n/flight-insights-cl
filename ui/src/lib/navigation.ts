// The Navigation and GNSS tab of one flight, as the engine's flight.navigation returns it,
// and the pure helpers behind it (spec: User interface, "The Navigation and GNSS view";
// mock-up Navigation): GNSS events, outages and drills, how the aircraft coped with each,
// the evidence each receiver gave, the readiness to fly without GNSS, and what would get
// it home, ranked. Every status and rank here is the engine's; these helpers only word
// and place it. Times are seconds after arming, distances relative metres from the
// analysis; the mitigations' holds and drift come from the platform's analyses.

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
  dead_reckoning_s: number | null; // when it began dead reckoning on the wind alone
  resumed_s: number | null;
  source_set: number | null;
  failsafes: string[];
  mode: string | null;
  mode_reason: string | null;
  // drift_m_per_min only where flow or vision corrected the velocity: on dead reckoning
  // the drift grows with the cube of the time, so the engine gives the minutes alone
  drift: { drift_m: number; dead_reckoning: boolean; drift_m_per_min?: number; minutes_to_tolerance?: number } | null;
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
  mitigations: string[]; // the ranked mitigations that answer it
}

/** A source in the catalogue: a wiki page (its URL and .rst path) or the 4.7.0 source. */
export interface Source {
  id: string;
  title: string;
  url: string;
  path: string;
  lines?: string;
}

/** A claim from the catalogue, with the sources it rests on. */
export interface Cited {
  text: Text;
  sources: string[];
}

/** Outages of one kind, as the fleet measured them. */
export interface Side {
  outages: number;
  hold_s: number; // the shortest time it kept a position it could fly on
  throughout: boolean; // every one kept a position all through: the hold is a lower bound
  minutes_to_tolerance: number | null; // the soonest the estimate was the landing tolerance off
  scope: "airframe" | "platform" | null;
}

export type WorkKind = "settings" | "calibration" | "hardware" | "companion_computer" | "research";
export type Rule = "goal" | "measured" | "expected" | "measured_no_gain";
export type Level = "keeps_position" | "buys_time" | "keeps_heading" | "pilot_control" | "fewer_losses";

/** One mitigation, ranked by the engine (mitigations.py) for this flight's setup. */
export interface Mitigation {
  id: string;
  kind: WorkKind; // the work it takes from scratch
  left: WorkKind; // the work left on this airframe, which the ranking uses
  status: "missing" | "partial" | "no_switch" | "on_switch";
  on_sets: number[]; // usable source sets that use it already
  grade: "documented" | "research";
  title: Text;
  for_goal: Text;
  makes_goal: boolean;
  rule: Rule; // the ranking rule that placed it
  basis: "measured" | "expected";
  measured: { scope: "airframe" | "platform" | null; with: Side | null; without: Side | null } | null;
  expected: { level: Level; text: Text; sources: string[] };
  bounds: string[];
  needs: Cited[];
  limits: Cited[];
  closes: { areas: { area: Area; status: AreaStatus }[]; findings: FindingRef[] };
  steps: { kind: ProcedureStep["kind"]; text: Text }[];
  sources: Source[];
}

export interface Drill {
  about: Text;
  measured: string[]; // the airframe's flights that measured a drill
  offered: boolean; // the airframe's plan offers it
  steps: { kind: ProcedureStep["kind"]; text: Text }[];
  sources: Source[];
}

export interface Mitigations {
  goal_now: boolean | null; // the goal with this flight's parameters
  items: Mitigation[];
  drill: Drill;
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
  mitigations: Mitigations;
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
  } else if (row.id === "drift" && typeof v.minutes === "number") {
    text += translate(locale, v.dead_reckoning === true ? "nav.cope.drift.cube" : "nav.cope.drift.minutes", {
      minutes: num(v.minutes, locale),
    });
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

/** The goal's status: met on a position it can trust, met on dead reckoning (its position
 * drifts on the way: a caution), or not met. */
export function goalLook(
  met: boolean,
  by: "position" | "dead_reckoning" | null = null,
): { status: Status; label: MessageKey } {
  if (!met) return { status: "out", label: "nav.goal.notMet" };
  return by === "dead_reckoning"
    ? { status: "caution", label: "nav.goal.metDeadReckoning" }
    : { status: "ok", label: "nav.goal.met" };
}

/** A procedure's own steps, as the shared step list shows them. */
export function actionSteps(a: { steps: Action["steps"] }): ProcedureStep[] {
  return a.steps.map((s) => ({ kind: s.kind, text: s.text, params: [], step: null, checks: [] }));
}

// --- what would get it home --------------------------------------------------------------

/** The work a mitigation leaves, as its badge says it. */
export function workLabel(kind: WorkKind, locale: Locale): string {
  return translate(locale, `nav.work.${kind}` as MessageKey);
}

/** Why it is where it is in the ranking. */
export function ruleLabel(rule: Rule, locale: Locale): string {
  return translate(locale, `nav.rule.${rule}` as MessageKey);
}

/** What is already there, and what is left, when part of it is in place. */
export function statusText(m: Mitigation, v: NavigationView, locale: Locale): string | null {
  const sets = m.on_sets.map((n) => formatNumber(n, locale)).join(", ");
  const switching = v.readiness.find((r) => r.area === "switching");
  const channels = (switching?.values.switch_channels ?? []) as number[];
  if (m.status === "on_switch") {
    return translate(locale, "nav.mit.status.on_switch", { sets, channel: channels[0] ?? "–" });
  }
  if (m.status === "no_switch") return translate(locale, "nav.mit.status.no_switch", { sets });
  if (m.status === "partial") return translate(locale, "nav.mit.status.partial", { work: workLabel(m.left, locale) });
  return null;
}

function sideText(s: Side, locale: Locale): string {
  const vars = { hold: formatNumber(s.hold_s, locale), count: s.outages };
  let text = translate(locale, s.throughout ? "nav.mit.side.throughout" : "nav.mit.side.hold", vars);
  if (s.minutes_to_tolerance !== null) {
    text += translate(locale, "nav.mit.side.minutes", { minutes: formatNumber(s.minutes_to_tolerance, locale) });
  }
  return text;
}

/** Where a measurement was flown. */
function scopeText(scope: Side["scope"], locale: Locale): string {
  return translate(locale, scope === "airframe" ? "nav.mit.scope.airframe" : "nav.mit.scope.platform");
}

/** Its benefit: measured with and without it where the fleet flew both, else the
 * catalogue's expectation, labelled which, with any one side that was measured. */
export function benefitText(m: Mitigation, locale: Locale): { label: string; lines: string[] } {
  const w = m.measured?.with ?? null;
  const wo = m.measured?.without ?? null;
  if (m.basis === "measured" && w && wo) {
    return {
      label: translate(locale, "nav.mit.measured", { scope: scopeText(m.measured?.scope ?? null, locale) }),
      lines: [
        translate(locale, "nav.mit.with", { side: sideText(w, locale) }),
        translate(locale, "nav.mit.without", { side: sideText(wo, locale) }),
      ],
    };
  }
  const lines = [m.expected.text[locale]];
  if (w) lines.push(translate(locale, "nav.mit.withOnly", { side: sideText(w, locale), scope: scopeText(w.scope, locale) }));
  if (wo && m.measured) {
    lines.push(translate(locale, "nav.mit.withoutOnly", { side: sideText(wo, locale), scope: scopeText(wo.scope, locale) }));
  }
  return { label: translate(locale, "nav.mit.expected"), lines };
}

/** A source as a line: its title, and where in the wiki or the firmware it is. */
export function sourceText(s: Source): string {
  return s.lines ? `${s.title} (${s.path}, ${s.lines})` : `${s.title} (${s.path})`;
}

/** The setup findings no ranked mitigation answers: they keep their own place. */
export function unanswered(v: NavigationView): Action[] {
  return v.actions.filter((a) => !a.mitigations.length);
}

/** Whether "Plan a GNSS-off drill" can open the airframe's plan with the drill in it. */
export function canPlanDrill(v: NavigationView): boolean {
  return Boolean(v.airframe_id && v.mitigations.drill.offered);
}

/** The drill as the procedure list shows it. */
export function drillSteps(d: Drill): ProcedureStep[] {
  return d.steps.map((s) => ({ kind: s.kind, text: s.text, params: [], step: null, checks: [] }));
}
