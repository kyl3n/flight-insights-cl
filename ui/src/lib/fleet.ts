// The fleet overview as the engine's fleet.overview returns it, and the helpers behind
// the Fleet view (spec: User interface, "Fleet"; mock-up Main): each airframe's status,
// tuning stage and key metrics, and what needs attention across the fleet.

import type { MessageKey } from "./i18n";
import { STAGES, type Baseline, type Severity, type Zone } from "./report";
import type { Status } from "./status";

export type AirframeStatus = "fix" | "inspect" | "ok" | "none";

export interface KeyMetric {
  metric: string;
  segment: string;
  unit: string;
  value: number;
  zone: Zone | null;
  drift: boolean;
}

export interface AirframeHealth {
  status: AirframeStatus;
  flights: number;
  team?: string | null;
  stage?: string | null; // the stage on offer; null when nothing is left to do
  planned?: boolean; // false: the flight had no parameters, so no plan and no stage
  counts?: Partial<Record<Severity, number>>;
  current?: boolean; // analysed with this engine and knowledge
  metrics?: KeyMetric[];
  last?: { flight: string; utc_us: number | null; duration_s: number | null; payload: string | null };
}

interface AttentionBase {
  airframe_id: string;
  airframe: string;
  team?: string | null;
  flight: string;
  utc_us: number | null;
  severity: "safety" | "out" | "caution";
}

export interface AttentionFinding extends AttentionBase {
  kind: "finding";
  code: string;
  analyzer: string;
  params: Record<string, unknown>;
  finding: string;
}

export interface AttentionDrift extends AttentionBase {
  kind: "drift";
  metric: string;
  segment: string;
  unit: string;
  value: number;
  baseline: Baseline | null;
}

export type Attention = AttentionFinding | AttentionDrift;

export interface FleetOverview {
  airframes: Record<string, AirframeHealth>;
  attention: Attention[];
}

export type Filter = "all" | "attention" | "tuning" | "healthy";

/** The team filter: every team, the airframes with no team, or one team by name. */
export type TeamFilter = { kind: "all" } | { kind: "none" } | { kind: "team"; team: string };

export function inTeam(team: string | null | undefined, f: TeamFilter): boolean {
  if (f.kind === "all") return true;
  if (f.kind === "none") return !team;
  return team === f.team;
}

/** The teams the airframes name, in order, and whether any airframe names none. */
export function teamsOf(airframes: { team?: string | null }[]): { teams: string[]; unnamed: boolean } {
  const teams = [...new Set(airframes.map((a) => a.team).filter((t): t is string => !!t))].sort((a, b) => a.localeCompare(b));
  return { teams, unnamed: airframes.some((a) => !a.team) };
}

/** The status shape and word an airframe is shown with. */
export function statusLook(status: AirframeStatus): { status: Status; label: MessageKey } {
  switch (status) {
    case "fix":
      return { status: "out", label: "fleet.status.fix" };
    case "inspect":
      return { status: "caution", label: "fleet.status.inspect" };
    case "ok":
      return { status: "ok", label: "fleet.status.ok" };
    default:
      return { status: "info", label: "fleet.status.none" };
  }
}

/** Whether an airframe shows under a filter: attention is anything not healthy and
 * flown; tuning is a plan with a stage still on offer; healthy is neither. */
export function keep(filter: Filter, h: AirframeHealth | undefined): boolean {
  const status = h?.status ?? "none";
  const tuning = typeof h?.stage === "string";
  switch (filter) {
    case "attention":
      return status === "fix" || status === "inspect";
    case "tuning":
      return tuning;
    case "healthy":
      return status === "ok" && !tuning;
    default:
      return true;
  }
}

const URGENCY: Record<AirframeStatus, number> = { fix: 0, inspect: 1, ok: 2, none: 3 };

/** Airframes most urgent first, then by name. */
export function byUrgency<T extends { id: string; name: string }>(
  rows: T[],
  health: Record<string, AirframeHealth>,
): T[] {
  return [...rows].sort(
    (a, b) =>
      URGENCY[health[a.id]?.status ?? "none"] - URGENCY[health[b.id]?.status ?? "none"] ||
      a.name.localeCompare(b.name),
  );
}

/** Where a stage sits in ArduPilot's tuning order, 1-based, out of how many. */
export function stagePosition(stage: string): { at: number; of: number } | null {
  const i = (STAGES as readonly string[]).indexOf(stage);
  return i < 0 ? null : { at: i + 1, of: STAGES.length };
}

/** Findings worth a count on the card: everything but information. */
export function findingCount(h: AirframeHealth): number {
  const c = h.counts ?? {};
  return (c.safety ?? 0) + (c.out ?? 0) + (c.caution ?? 0);
}
