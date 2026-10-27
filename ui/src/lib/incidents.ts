// A flight's incident timeline, as the engine's flight.timeline returns it, and the pure
// helpers behind its screen (spec: Whole-system incident timeline; Failsafe causes and RF
// early warning, "Did the failsafe work?", "Lead-time analysis", "Near misses"; mock-up
// Incidents). Every source on one clock, in seconds after arming. The first fault, the
// failsafe verdicts, their timing, lead times and near misses are the engine's findings
// and evidence: nothing here judges an event, it only lays them out and matches them up.

import type { Locale } from "./i18n";
import { fill, type Analysis, type EvidenceRow, type FindingRow, type Registry, type Text } from "./report";
import type { Status } from "./status";

/** An abnormal event, with the facts the engine read for it (which depend on its kind). */
export interface TimelineEvent {
  at_s: number;
  kind: string; // rules/incident.json's names: "link_outage", "error:FAILSAFE_GCS", "mode:GCS_FAILSAFE" ...
  words: Text;
  to_s?: number; // for one that lasts
  seconds?: number; // a link outage or the ground station unheard: how long
  what?: string; // the receiver's failsafe throttle: the channel and the setting
  monitor?: string; // the battery under its low voltage: which monitor, and the voltage
  under?: number;
  code?: number; // an error's code
  name?: string; // a parameter changed in flight
  from?: number;
  to?: number;
  mode?: string; // a mode change a failsafe made
  command?: number; // a command refused: its number and result
  result?: number;
}

export interface ModeChange {
  at_s: number;
  mode: string;
  reason: string; // ModeReason, ArduPilot's own name
}

export interface Span<T> {
  from_s: number;
  to_s: number;
  value: T;
}

export interface Command {
  at_s: number;
  command: string;
  result: string;
  sender: number; // the sender's MAVLink system id
}

export interface Warning {
  at_s: number;
  severity: number; // MAV_SEVERITY: 0 emergency … 4 warning
  text: string; // the ground station's own text; it stays on this laptop
}

export interface Lanes {
  link: TimelineEvent[];
  receiver: TimelineEvent[];
  errors: TimelineEvent[];
  battery: TimelineEvent[];
  gnss_lost: TimelineEvent[];
  params: TimelineEvent[];
  mode?: ModeChange[];
  pilot_overrides?: Span<boolean>[];
  commands?: Command[];
  warnings: Warning[];
  gnss_fix?: Span<number>[];
}

export interface FirstFault {
  chain: TimelineEvent[]; // from the first fault to the main event
  main: TimelineEvent;
  explained: TimelineEvent[]; // later events the main event leads to
}

export interface Timeline {
  flight_id: string;
  armed_s: number; // disarming, in seconds after arming
  lanes: Lanes;
  first_fault: FirstFault | null;
}

// --- lanes ------------------------------------------------------------------------------

export type LaneId =
  | "link"
  | "receiver"
  | "pilot"
  | "errors"
  | "mode"
  | "commands"
  | "warnings"
  | "gnss"
  | "battery"
  | "params";

export type Tone = Status | "neutral";

/** What a mark stands for, as the inspector shows it. */
export type Detail =
  | { type: "event"; lane: LaneId | null; event: TimelineEvent }
  | { type: "mode"; change: ModeChange; to_s: number }
  | { type: "command"; command: Command }
  | { type: "warning"; warning: Warning }
  | { type: "fix"; span: Span<number> }
  | { type: "override"; span: Span<boolean> };

export interface Mark {
  key: string;
  from: number; // seconds after arming
  to: number | null; // null: an instant
  tone: Tone;
  glyph: string | null; // an instant's shape: ● ▲ ◆ ■
  label: string | null; // drawn beside it; the rest are named in the inspector
  title: string; // its name on hover and for screen readers
  detail: Detail;
}

export interface Lane {
  id: LaneId;
  marks: Mark[]; // in drawing order: the most urgent last, on top
}

/** How urgent a tone is, for drawing order: marks at the same moment show the most
 * urgent on top, and its label first. */
export const TONE_RANK: Record<Tone, number> = { neutral: 0, info: 1, ok: 2, caution: 3, out: 4 };
const byUrgency = (l: Lane): Lane => ({
  id: l.id,
  marks: l.marks.map((m, i) => ({ m, i })).sort((a, b) => TONE_RANK[a.m.tone] - TONE_RANK[b.m.tone] || a.i - b.i).map((x) => x.m),
});

/** Who changed the mode: a person (from their radio or the ground station) or the
 * aircraft itself (a failsafe, a check, a mission's end). */
export const PERSON_REASONS = ["RC_COMMAND", "GCS_COMMAND", "INITIALISED", "UNKNOWN"];
const FAILURES = ["error:FAILSAFE_", "error:CRASH_CHECK"]; // the main events the engine looks for first

export const sameEvent = (a: TimelineEvent, b: TimelineEvent): boolean =>
  a.kind === b.kind && Math.abs(a.at_s - b.at_s) < 0.005;

/** An event's place in the first fault: a step of its chain, the main event, explained
 * by it, or none of these. */
export function role(
  ff: FirstFault | null,
  e: TimelineEvent,
): { kind: "first" | "step" | "main" | "explained"; step: number; of: number } | null {
  if (!ff) return null;
  const of = ff.chain.length;
  const i = ff.chain.findIndex((c) => sameEvent(c, e));
  if (i === 0 && of > 1) return { kind: "first", step: 1, of };
  if (i >= 0 && i === of - 1) return { kind: "main", step: of, of };
  if (i >= 0) return { kind: "step", step: i + 1, of };
  if (ff.explained.some((c) => sameEvent(c, e))) return { kind: "explained", step: 0, of };
  return null;
}

export function detailKey(d: Detail): string {
  switch (d.type) {
    case "event":
      return `event:${d.event.kind}@${d.event.at_s}`;
    case "mode":
      return `mode:${d.change.mode}@${d.change.at_s}`;
    case "command":
      return `command:${d.command.command}@${d.command.at_s}`;
    case "warning":
      return `warning:${d.warning.text}@${d.warning.at_s}`;
    case "fix":
      return `fix:${d.span.value}@${d.span.from_s}`;
    case "override":
      return `override@${d.span.from_s}`;
  }
}

function eventTone(e: TimelineEvent, ff: FirstFault | null): Tone {
  if (FAILURES.some((p) => e.kind.startsWith(p))) return "out";
  return role(ff, e) ? "out" : "caution";
}

/** The lanes in the order the screen draws them (the mock-up's, with the receiver beside
 * the link and parameter changes last). A lane the log cannot fill is left out; one it
 * fills with nothing abnormal stays, empty. */
export function lanes(
  tl: Timeline,
  locale: Locale,
  text: { firstFault: string; seconds: (s: number) => string },
): Lane[] {
  const ff = tl.first_fault;
  const L = tl.lanes;
  const out: Lane[] = [];
  const labelled = (e: TimelineEvent) => {
    const r = role(ff, e);
    if (r?.kind === "first") return `${e.words[locale]} · ${text.firstFault}`;
    return r ? e.words[locale] : null;
  };
  const events = (id: LaneId, list: TimelineEvent[], label: (e: TimelineEvent) => string | null): Lane => ({
    id,
    marks: list.map((e, i) => ({
      key: `${id}:${i}`,
      from: e.at_s,
      to: e.to_s ?? null,
      tone: eventTone(e, ff),
      glyph: e.to_s === undefined ? (id === "errors" ? "◆" : "▲") : null,
      label: label(e),
      title: e.words[locale],
      detail: { type: "event", lane: id, event: e },
    })),
  });
  const lasting = (e: TimelineEvent) =>
    labelled(e) ?? (e.to_s !== undefined ? text.seconds(e.to_s - e.at_s) : null);
  out.push(events("link", L.link, lasting));
  out.push(events("receiver", L.receiver, lasting));
  if (L.pilot_overrides) {
    out.push({
      id: "pilot",
      marks: L.pilot_overrides.map((s, i) => ({
        key: `pilot:${i}`, from: s.from_s, to: s.to_s, tone: "info", glyph: null, label: null,
        title: `${text.seconds(s.from_s)} – ${text.seconds(s.to_s)}`, detail: { type: "override", span: s },
      })),
    });
  }
  out.push(events("errors", L.errors, (e) => labelled(e) ?? e.words[locale]));
  if (L.mode) {
    out.push({ id: "mode", marks: modeSpans(L.mode, tl.armed_s, ff) });
  }
  if (L.commands) {
    out.push({
      id: "commands",
      marks: L.commands.map((c, i) => {
        const tone = commandTone(c.result);
        return {
          key: `commands:${i}`, from: c.at_s, to: null, tone, glyph: "●",
          label: tone === "neutral" ? null : `${c.command} ${c.result}`,
          title: `${c.command}: ${c.result}`, detail: { type: "command", command: c },
        };
      }),
    });
  }
  out.push({
    id: "warnings",
    marks: L.warnings.map((w, i) => ({
      key: `warnings:${i}`, from: w.at_s, to: null, tone: w.severity <= 3 ? "out" : "caution", glyph: "▲",
      label: null, title: `“${w.text}”`, detail: { type: "warning", warning: w },
    })),
  });
  if (L.gnss_fix || L.gnss_lost.length) {
    const fixes: Mark[] = (L.gnss_fix ?? []).map((s, i) => ({
      key: `gnss:fix:${i}`, from: s.from_s, to: s.to_s, tone: s.value >= 3 ? "ok" : "neutral", glyph: null,
      label: null, title: `${s.value}`, detail: { type: "fix", span: s },
    }));
    out.push({ id: "gnss", marks: [...fixes, ...events("gnss", L.gnss_lost, lasting).marks] });
  }
  out.push(events("battery", L.battery, lasting));
  out.push(events("params", L.params, labelled));
  return out.map(byUrgency);
}

/** The mode as spans, from each change to the next: a repeated request for the mode it
 * was already in is no change. The aircraft's own changes are marked, a person's not. */
export function modeSpans(changes: ModeChange[], armedS: number, ff: FirstFault | null): Mark[] {
  const kept: ModeChange[] = [];
  for (const c of [...changes].sort((a, b) => a.at_s - b.at_s)) {
    if (kept[kept.length - 1]?.mode !== c.mode) kept.push(c);
  }
  return kept.map((c, i) => {
    const to = kept[i + 1]?.at_s ?? Math.max(armedS, c.at_s);
    const own = !PERSON_REASONS.includes(c.reason);
    const inChain = ff !== null && [...ff.chain, ...ff.explained].some(
      (e) => e.kind === `mode:${c.reason}` && Math.abs(e.at_s - c.at_s) < 0.05,
    );
    return {
      key: `mode:${i}`, from: c.at_s, to, tone: inChain ? "out" : own ? "caution" : "neutral", glyph: null,
      label: c.mode, title: c.mode, detail: { type: "mode", change: c, to_s: to },
    };
  });
}

/** A command's result as a tone: accepted or still running is the ordinary case; not
 * supported is a ground station probing; anything else was refused. */
export function commandTone(result: string): Tone {
  if (result === "accepted" || result === "in progress") return "neutral";
  if (result === "not supported" || result === "cancelled") return "info";
  return "caution";
}

// --- the time axis ----------------------------------------------------------------------

export interface Axis {
  start: number;
  end: number;
  ticks: number[];
}

const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

/** One axis for every lane: from arming, or earlier when something was logged before it,
 * to disarming, or later when something ran past it, with a little room either side so a
 * mark at the very end shows whole; ticks at a round step. */
export function axis(tl: Timeline, wanted = 7): Axis {
  const times: number[] = [0, tl.armed_s];
  const L = tl.lanes;
  for (const list of [L.link, L.receiver, L.errors, L.battery, L.gnss_lost, L.params]) {
    for (const e of list) times.push(e.at_s, e.to_s ?? e.at_s);
  }
  for (const m of L.mode ?? []) times.push(m.at_s);
  for (const c of L.commands ?? []) times.push(c.at_s);
  for (const w of L.warnings) times.push(w.at_s);
  for (const s of [...(L.pilot_overrides ?? []), ...(L.gnss_fix ?? [])]) times.push(s.from_s, s.to_s);
  const finite = times.filter(Number.isFinite);
  let start = Math.min(...finite);
  let end = Math.max(...finite);
  if (end <= start) end = start + 1;
  const room = (end - start) * 0.015;
  start -= room;
  end += room;
  const step = STEPS.find((s) => (end - start) / s <= wanted) ?? 3600;
  const ticks: number[] = [];
  for (let v = Math.ceil(start / step) * step; v <= end + 1e-9; v += step) ticks.push(Math.round(v * 1000) / 1000);
  return { start, end, ticks };
}

/** Where a time falls on the axis, in percent of its width. */
export function at(a: Axis, s: number): number {
  return Math.min(100, Math.max(0, ((s - a.start) / (a.end - a.start)) * 100));
}

/** Words as a sentence's start: the engine words an event in lower case, to fit its
 * finding texts. */
export function sentence(words: string): string {
  return words.charAt(0).toLocaleUpperCase() + words.slice(1);
}

/** A time in seconds, to a tenth; under a second, to a hundredth, so a failsafe's short
 * wait still reads. */
export function formatSeconds(s: number, locale: Locale): string {
  const digits = Math.abs(s) < 1 ? 2 : 1;
  const text = new Intl.NumberFormat(locale === "uk" ? "uk-UA" : "en-GB", { maximumFractionDigits: digits }).format(
    Math.round(s * 10 ** digits) / 10 ** digits,
  );
  return `${text} ${locale === "uk" ? "с" : "s"}`;
}

export interface Tag {
  text: string;
  flip: boolean; // drawn to the left of its anchor, near the track's right end
  anchor: number; // seconds after arming
}

/** Where each mark's label goes on its lane, so none covers another: left to right, a
 * label that would run past the track's end is drawn to the left of its mark (or of its
 * span's end), labels of marks at the same moment are joined, and one that would still
 * cover another is left to the inspector. `trackPx` is the track's drawn width. */
export function placeTags(marks: Mark[], a: Axis, trackPx: number, charPx = 6.2, gapPx = 10): Map<string, Tag> {
  const out = new Map<string, Tag>();
  const placed: { key: string; x0: number; x1: number; anchorPx: number; flip: boolean }[] = [];
  const px = (s: number) => (at(a, s) / 100) * trackPx;
  for (const m of [...marks].sort((x, y) => x.from - y.from || TONE_RANK[y.tone] - TONE_RANK[x.tone])) {
    if (!m.label) continue;
    const w = m.label.length * charPx;
    const flip = px(m.from) + 6 + w > trackPx;
    const anchor = flip && m.to !== null ? m.to : m.from;
    const ax = px(anchor);
    const [x0, x1] = flip ? [ax - 6 - w, ax - 6] : [ax + 6, ax + 6 + w];
    const hit = placed.find((p) => x0 < p.x1 + gapPx && x1 > p.x0 - gapPx);
    if (hit) {
      const tag = out.get(hit.key);
      if (tag && Math.abs(hit.anchorPx - ax) < 4) {
        tag.text = `${tag.text} · ${m.label}`;
        const more = (m.label.length + 3) * charPx;
        if (hit.flip) hit.x0 -= more;
        else hit.x1 += more;
      }
      continue;
    }
    placed.push({ key: m.key, x0, x1, anchorPx: ax, flip });
    out.set(m.key, { text: m.label, flip, anchor });
  }
  return out;
}

// --- the first fault --------------------------------------------------------------------

export const INCIDENT_CODES = ["incident.first_fault", "incident.unexplained"];

export function incidentFinding(a: Analysis | null): FindingRow | null {
  return a?.findings.find((f) => INCIDENT_CODES.includes(f.code)) ?? null;
}

/** The finding's own params, with the events it names in the screen's language: the
 * engine words each event in both, and writes the finding's params in English. */
export function incidentParams(f: FindingRow, ff: FirstFault | null, locale: Locale): Record<string, unknown> {
  const first = ff?.chain[0];
  if (!ff || !first) return f.params;
  const w = (e: TimelineEvent) => e.words[locale];
  return {
    ...f.params,
    first: w(first),
    main: w(ff.main),
    chain: ff.chain.map(w).join(" → "),
  };
}

export function incidentText(
  reg: Registry,
  f: FindingRow,
  ff: FirstFault | null,
  locale: Locale,
): { title: string; action: string } {
  const text = reg.findings[f.code];
  const params = incidentParams(f, ff, locale);
  return text
    ? { title: fill(text.title[locale], params, locale), action: fill(text.action[locale], params, locale) }
    : { title: f.code, action: "" };
}

// --- did each failsafe work? ------------------------------------------------------------

/** What the failsafe analyzer read for one failsafe (its evidence's values). */
export interface FailsafeFacts {
  failsafe: string; // FAILSAFE_GCS, FAILSAFE_RADIO, FAILSAFE_BATT, FAILSAFE_EKFINAV
  at_s: number;
  lasted_s: number | null; // until it was logged resolved; null when it was not
  mode_before: string;
  response: string; // a mode's name, "continue" or "disarm"
  wanted: string; // what the settings asked for first
  allowed: string[]; // every response the settings and the log allow, first the wanted
  setting: string; // the setting(s) that chose the action, comma separated
  setting_value: string; // their values, in the same order
  fs_options: number;
  left_open: string[]; // what the log left open, where it mattered
  responded_after_s: number | null;
  loss_from: string | null; // what the loss's start was read from, in words; null: not measurable
  loss_began_s: number | null;
  waited_s: number | null;
  wait_s: number | null;
  late_s: number | null;
}

export type Verdict = "as_configured" | "fell_back" | "unexpected";
export const VERDICTS: Verdict[] = ["as_configured", "fell_back", "unexpected"];

export interface Lead {
  finding: FindingRow | null; // failsafe.lead_time, or failsafe.unwarned
  evidence: EvidenceRow;
  precursors: { id: string; lead_s: number | null }[]; // warned first, earliest first
}

export interface FailsafeCheck {
  finding: FindingRow;
  verdict: Verdict;
  evidence: EvidenceRow;
  facts: FailsafeFacts;
  late: FindingRow | null;
  lead: Lead | null;
  others: FailsafeFacts[]; // other failsafes in force when it fired
}

const verdictOf = (code: string): Verdict | null => {
  const v = code.replace(/^failsafe\./, "");
  return code.startsWith("failsafe.") && (VERDICTS as string[]).includes(v) ? (v as Verdict) : null;
};

/** Each failsafe the engine judged, in the order it fired, with its timing, its lead time
 * and the other failsafes in force then. */
export function failsafeChecks(a: Analysis): FailsafeCheck[] {
  const evidence = new Map(a.evidence.map((e) => [e.id, e]));
  const checks: FailsafeCheck[] = [];
  for (const f of a.findings) {
    const verdict = verdictOf(f.code);
    const ev = evidence.get(f.evidence[0] ?? "");
    if (!verdict || !ev) continue;
    checks.push({
      finding: f,
      verdict,
      evidence: ev,
      facts: ev.values as unknown as FailsafeFacts,
      late: a.findings.find((x) => x.code === "failsafe.late" && x.evidence.includes(ev.id)) ?? null,
      lead: leadFor(a, ev.values as unknown as FailsafeFacts),
      others: [],
    });
  }
  checks.sort((x, y) => x.facts.at_s - y.facts.at_s);
  for (const c of checks) {
    c.others = checks
      .filter((o) => o !== c && o.facts.at_s <= c.facts.at_s)
      .filter((o) => o.facts.lasted_s === null || o.facts.at_s + o.facts.lasted_s > c.facts.at_s)
      .map((o) => o.facts);
  }
  return checks;
}

/** The lead-time evidence of one failsafe: the precursors analyzer names it by the
 * failsafe's kind and time ("precursor:lead:gcs:14.9"). */
export function leadFor(a: Analysis, facts: FailsafeFacts): Lead | null {
  const short = facts.failsafe.replace(/^FAILSAFE_/, "").toLowerCase();
  const ev = a.evidence.find((e) => {
    if (!e.id.startsWith(`precursor:lead:${short}:`)) return false;
    const t = e.values.at_s;
    return typeof t === "number" && Math.abs(t - facts.at_s) < 0.05;
  });
  if (!ev) return null;
  const precursors = Object.entries(ev.values)
    .filter(([k]) => k.endsWith("_lead_s"))
    .map(([k, v]) => ({ id: k.slice(0, -"_lead_s".length), lead_s: typeof v === "number" ? v : null }))
    .sort((x, y) => (y.lead_s ?? -Infinity) - (x.lead_s ?? -Infinity) || x.id.localeCompare(y.id));
  const finding =
    a.findings.find((f) => (f.code === "failsafe.lead_time" || f.code === "failsafe.unwarned") && f.evidence.includes(ev.id)) ??
    null;
  return { finding, evidence: ev, precursors };
}

/** The setting(s) that chose a failsafe's action, each with its value. */
export function settingPairs(setting: string, value: string): [string, string][] {
  const names = setting.split(", ");
  const values = value.split(", ");
  return names.map((n, i) => [n, values[i] ?? ""]);
}

export function verdictLook(v: Verdict): Status {
  return v === "as_configured" ? "ok" : v === "fell_back" ? "caution" : "out";
}

/** The trigger's timing: late when the engine found it late, not measurable when the log
 * does not show the loss begin, else on time. */
export function timingLook(c: FailsafeCheck): { status: Status; key: "late" | "unmeasured" | "onTime" } {
  if (c.late) return { status: "caution", key: "late" };
  if (c.facts.loss_from === null || c.facts.waited_s === null) return { status: "info", key: "unmeasured" };
  return { status: "ok", key: "onTime" };
}

/** Losses no failsafe answered: one that should have fired and did not, or one switched
 * off, in the order the losses began. */
export function unanswered(a: Analysis): { finding: FindingRow; evidence: EvidenceRow | null }[] {
  const evidence = new Map(a.evidence.map((e) => [e.id, e]));
  return a.findings
    .filter((f) => f.code === "failsafe.missed" || f.code === "failsafe.off_during_loss")
    .map((f) => ({ finding: f, evidence: evidence.get(f.evidence[0] ?? "") ?? null }))
    .sort((x, y) => Number(x.finding.params.at_s ?? 0) - Number(y.finding.params.at_s ?? 0));
}

// --- near misses ------------------------------------------------------------------------

export type NearKind = "gcs" | "radio" | "battery";
const NEAR: Record<string, NearKind> = {
  "link.gcs_silence": "gcs",
  "link.rc_receiver_failsafes": "radio",
  "failsafe.battery_near_miss": "battery",
};

/** Close calls that did not trip: the link analyzer's for the ground station and the
 * receiver, the precursors analyzer's for the battery. When each came: the finding's
 * time, or its evidence's first. */
export function nearMisses(a: Analysis): { finding: FindingRow; kind: NearKind; at_s: number | null }[] {
  const evidence = new Map(a.evidence.map((e) => [e.id, e]));
  return a.findings
    .flatMap((f) => {
      const kind = NEAR[f.code];
      if (!kind) return [];
      const ev = evidence.get(f.evidence[0] ?? "");
      const t = f.params.at_s ?? ev?.values.first_s ?? ev?.values.from_s;
      return [{ finding: f, kind, at_s: typeof t === "number" ? t : null }];
    })
    .sort((x, y) => (x.at_s ?? Infinity) - (y.at_s ?? Infinity));
}
