// The tuning plan as the engine's recommend.plan returns it, and the helpers the Plan
// and Configuration review views share (spec: Recommendation engine; mock-ups Plan
// and ConfigReview). The engine decides stages, conflicts and validity; the views
// only show them and send a person's decisions back.

import type { MessageKey } from "./i18n";
import { STAGES, type Severity, type Text } from "./report";

export type RecStatus = "next" | "waiting" | "blocked" | "disputed";
export type RecordStatus =
  | "proposed" | "accepted" | "rejected" | "applied" | "superseded"
  | "confirmed" | "no effect" | "regressed";

export interface Note {
  reason: string;
  params: Record<string, unknown>;
  text: Text;
}

export interface RecSource {
  finding: string;
  analyzer: string;
  code: string;
  severity: Severity;
  why: Text;
  origin?: "measured" | "ai";
}

/** One step of a procedure, in the order a person does it. */
export interface ProcedureStep {
  kind:
    | "task" | "hardware" | "firmware" | "set" | "refresh" | "reboot_refresh" | "reboot"
    | "temporary" | "ground_check" | "fly" | "abort" | "verify" | "revert" | "undo";
  text: Text;
  params: string[]; // the firmware's own names it loads or names
  step: number | null; // the plan's file, for "set"
  checks: Record<string, unknown>[]; // what the app tests on the next flight
}

export interface PlanRec {
  param: string | null; // the firmware's own name; null for an action
  canonical: string | null;
  finding: string;
  current: number | null;
  proposed: number | null;
  target: number | null;
  stage: string;
  kind: "safety" | "stability" | "performance";
  status: RecStatus;
  step: number | null;
  why: Text;
  evidence: string[];
  expected_effect: Text & { simulated: boolean };
  confidence: string;
  risk: string;
  requires: string[];
  reboot: boolean;
  temporary?: boolean; // for the test flight only
  revert?: number | null; // what a test-flight setting goes back to, the firmware's units
  verify: Text | null;
  procedure?: ProcedureStep[];
  source: string;
  sources: RecSource[];
  notes: Note[];
  alternatives: { proposed: number; sources: RecSource[] }[];
  id?: string; // kept for the airframe
  record_status?: RecordStatus;
  record_version?: number;
}

export interface Plan {
  flight: string;
  vehicle: string;
  firmware: string;
  stage: string | null;
  recommendations: PlanRec[];
  steps: { number: number; params: string[]; after: "refresh" | "reboot_refresh" | "reboot" | "fly" }[];
  session?: ProcedureStep[]; // everything on offer, as one procedure
  held_back?: { code: string; flight: string; reason?: string; text: Text }[]; // not proposed again: diagnostics, or work shown done
}

export interface HistoryRow {
  id: string;
  param: string | null;
  proposed_value: number | null;
  status: RecordStatus;
  flight_id: string | null;
  graded_flight_id: string | null;
  updated_hlc: string;
  detail: {
    why?: Text;
    code?: string;
    param?: string | null;
    proposed?: number | null;
    grade?: { checks?: (Record<string, unknown> & { passed: boolean | null })[] };
  };
}

export type StageState = "clear" | "now" | "later";

/** Where each stage stands: before the one on offer it is clear, after it waits. */
export function stageStates(plan: Plan): { stage: string; state: StageState; count: number }[] {
  const gate = plan.stage === null ? STAGES.length : STAGES.indexOf(plan.stage as (typeof STAGES)[number]);
  return STAGES.map((stage, i) => ({
    stage,
    state: i < gate ? "clear" : i === gate ? "now" : "later",
    count: plan.recommendations.filter((r) => r.stage === stage && r.status !== "blocked").length,
  }));
}

/** On offer now (with disputed ones, which need a person), waiting, and blocked. */
export function partition(plan: Plan): { now: PlanRec[]; waiting: PlanRec[]; blocked: PlanRec[] } {
  return {
    now: plan.recommendations.filter((r) => r.status === "next" || r.status === "disputed"),
    waiting: plan.recommendations.filter((r) => r.status === "waiting"),
    blocked: plan.recommendations.filter((r) => r.status === "blocked"),
  };
}

export interface RecGroup {
  key: string;
  lead: PlanRec; // the first, whose reason, check and stage the card shows
  recs: PlanRec[];
}

/** One card per finding: the changes it proposes together, in plan order. An action
 * (no parameter) is its own card. */
export function groupByFinding(recs: PlanRec[]): RecGroup[] {
  const out: RecGroup[] = [];
  const byKey = new Map<string, RecGroup>();
  for (const r of recs) {
    const key = r.param === null ? `action|${r.finding}` : `${r.status}|${r.finding}`;
    const group = byKey.get(key);
    if (group) {
      group.recs.push(r);
    } else {
      const fresh = { key, lead: r, recs: [r] };
      byKey.set(key, fresh);
      out.push(fresh);
    }
  }
  return out;
}

/** A group's decision: the one every change in it shares, or "mixed". */
export function groupDecision(g: RecGroup): RecordStatus | "mixed" | "none" {
  const states = new Set(g.recs.map((r) => r.record_status ?? "proposed"));
  if (g.recs.some((r) => !r.id)) return "none";
  return states.size === 1 ? ([...states][0] as RecordStatus) : "mixed";
}

/** Accepted changes, and the ones on offer still to decide. */
export function decisionCounts(plan: Plan): { accepted: number; open: number } {
  const offered = plan.recommendations.filter((r) => r.status === "next" && r.param !== null);
  return {
    accepted: offered.filter((r) => r.record_status === "accepted").length,
    open: offered.filter((r) => !r.record_status || r.record_status === "proposed").length,
  };
}

/** Steps a person ticks off as they go: work on the aircraft and on the bench, and
 * the files they load. What to fly and check is read, not ticked. */
const TICKED = new Set<ProcedureStep["kind"]>([
  "task", "hardware", "firmware", "set", "refresh", "reboot_refresh", "reboot",
  "temporary", "ground_check", "revert",
]);

export function tickable(step: ProcedureStep): boolean {
  return TICKED.has(step.kind);
}

/** A step's label: what kind of step it is, in a few words. */
export function stepLabel(kind: ProcedureStep["kind"]): MessageKey {
  return `step.${kind}` as MessageKey;
}

/** The checks the next flight ran for a graded recommendation: how many passed, of
 * those it could judge; null when it ran none. */
export function checksPassed(row: HistoryRow): { passed: number; total: number } | null {
  const judged = (row.detail.grade?.checks ?? []).filter((c) => c.passed !== null);
  if (!judged.length) return null;
  return { passed: judged.filter((c) => c.passed).length, total: judged.length };
}

/** Whether any accepted change is for the test flight only, so a revert file exists. */
export function hasRevert(plan: Plan): boolean {
  return plan.recommendations.some(
    (r) => r.status === "next" && r.record_status === "accepted" && r.revert !== null && r.revert !== undefined,
  );
}

/** Ticked steps of a session card, kept on this laptop only. Storage can be missing or
 * refuse (private windows, previews); the card then simply starts unticked. */
export function loadTicks(key: string, store: Pick<Storage, "getItem"> | undefined = globalThis.localStorage): Set<number> {
  try {
    const raw = store?.getItem(key);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((n): n is number => typeof n === "number") : []);
  } catch {
    return new Set();
  }
}

export function saveTicks(
  key: string,
  ticks: Set<number>,
  store: Pick<Storage, "setItem"> | undefined = globalThis.localStorage,
): void {
  try {
    store?.setItem(key, JSON.stringify([...ticks].sort((a, b) => a - b)));
  } catch {
    // ticks are a convenience; nothing depends on them being kept
  }
}

/** The plan's recommendation that carries a finding, if any (for "Add to plan"). */
export function recFor(plan: Plan, findingId: string): PlanRec | undefined {
  return plan.recommendations.find(
    (r) => r.finding === findingId || r.sources.some((s) => s.finding === findingId),
  );
}

export const GRADES: RecordStatus[] = ["confirmed", "no effect", "regressed"];

/** The latest graded recommendations, newest first. */
export function outcomes(history: HistoryRow[], limit = 5): HistoryRow[] {
  return history
    .filter((h) => GRADES.includes(h.status))
    .sort((a, b) => (a.updated_hlc < b.updated_hlc ? 1 : -1))
    .slice(0, limit);
}
