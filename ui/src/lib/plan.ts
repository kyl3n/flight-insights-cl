// The tuning plan as the engine's recommend.plan returns it, and the helpers the Plan
// and Configuration review views share (spec: Recommendation engine; mock-ups Plan
// and ConfigReview). The engine decides stages, conflicts and validity; the views
// only show them and send a person's decisions back.

import type { Locale } from "./i18n";
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
  verify: Text | null;
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
}

export interface HistoryRow {
  id: string;
  param: string | null;
  proposed_value: number | null;
  status: RecordStatus;
  flight_id: string | null;
  graded_flight_id: string | null;
  updated_hlc: string;
  detail: { why?: Text; code?: string; param?: string | null; proposed?: number | null };
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

/** What the test flight checks: each stage's verification once, and every action a
 * person must do first. */
export function testCard(plan: Plan, locale: Locale): { first: string[]; checks: string[] } {
  const now = plan.recommendations.filter((r) => r.status === "next");
  const first = now.filter((r) => r.param === null).map((r) => r.why[locale]);
  const checks = [...new Set(now.flatMap((r) => (r.verify ? [r.verify[locale]] : [])))];
  return { first, checks };
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
