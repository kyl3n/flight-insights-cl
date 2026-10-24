// The AI layer as the interface sees it (spec: AI layer): what came of a flight's
// exchange, the shell's AI status, the key and the cap, and what left this machine.
// The interface never holds the key after it is pasted, never sees a tripwire, and
// shows nothing an exchange proposes unless the engine checked it first.

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { inDesktopApp } from "./shell";

export type Outcome = "accepted" | "invalid" | "declined" | "failed";

export interface Reply {
  summary: string;
  root_causes: { title: string; explanation: string; refs: string[] }[];
  recommendations: { param: string; value: number; confidence: string; reason: string; refs: string[] }[];
  disagreements: { ref: string; view: string }[];
  questions: { question: string; why: string }[];
  tests_to_fly: { test: string; look_for: string }[];
}

export interface Verdict {
  param: string;
  value: number;
  status: string; // next, waiting, disputed, blocked, outranked, refused, unchanged
  notes: { reason: string; text?: Record<string, string> }[];
}

export interface Exchange {
  id: string;
  packet_id: string;
  flight_id: string | null;
  model: string;
  outcome: Outcome;
  reasons: string[];
  reply: Reply | null;
  recommendations: Verdict[];
  tool_calls: number;
  cost_usd: number;
  created: string;
  run_id?: string | null;
  refs?: Record<string, string>; // "F1" -> finding id, "E1" -> evidence id
}

export interface AiStatus {
  key_set: boolean;
  key_rejected: boolean;
  monthly_cap_usd: number;
  language: string;
  month: { month: string; cost_usd: number; exchanges: number } | null;
  queued: number;
  gate: "pending" | "ready" | "blocked";
}

/** What the worker said about one flight (`ai-progress`). */
export interface Progress {
  flight_id: string;
  state: "gate" | "no_key" | "sending" | "waiting" | "done" | "held" | "failed";
  detail: unknown;
}

export interface SentPacket {
  id: string;
  created_hlc: string;
  flight_id: string | null;
  purpose: "analysis" | "chat" | "explain" | "tool";
  parent_id: string | null;
  sha256: string;
  packet_json: string;
  engine_verdict: "pass" | "refused";
  engine_reasons: { path: string; rule: string }[];
  shell_verdict: "pass" | "refused" | null;
  shell_reasons: { path: string; rule: string }[];
  sent: boolean;
  sent_hlc: string | null;
}

export async function aiStatus(): Promise<AiStatus | null> {
  if (!inDesktopApp()) return null;
  return invoke<AiStatus>("ai_status");
}

export async function aiKeySet(key: string): Promise<void> {
  return invoke("ai_key_set", { key });
}

export async function aiKeyClear(): Promise<void> {
  return invoke("ai_key_clear");
}

export async function aiSettingsSet(monthlyCapUsd: number, language: string): Promise<void> {
  if (!inDesktopApp()) return;
  return invoke("ai_settings_set", { monthlyCapUsd, language });
}

export async function aiSendNow(flightId: string): Promise<void> {
  return invoke("ai_send_now", { flightId });
}

export async function onAiProgress(handler: (p: Progress) => void): Promise<UnlistenFn> {
  if (!inDesktopApp()) return () => {};
  return listen<Progress>("ai-progress", (e) => handler(e.payload));
}

/** The wall-clock time of a clock stamp ("<ms>-<counter>-<laptop>"). */
export function hlcTime(hlc: string | null): Date | null {
  const ms = Number((hlc ?? "").split("-")[0]);
  return Number.isFinite(ms) && ms > 0 ? new Date(ms) : null;
}

export type SectionState =
  | { kind: "off"; why: "browser" | "gate" | "blocked" | "no_key" | "key_rejected" }
  | { kind: "pending" }
  | { kind: "held" }
  | { kind: "shown"; exchange: Exchange }
  | { kind: "problem"; exchange: Exchange };

/** What the flight report's AI section shows. `progress` is the worker's latest word
 * on this flight, if any came while the report was open. */
export function sectionState(
  status: AiStatus | null,
  exchange: Exchange | null,
  progress: Progress | null,
): SectionState {
  if (status === null) return { kind: "off", why: "browser" };
  if (status.gate === "blocked") return { kind: "off", why: "blocked" };
  if (status.key_rejected) return { kind: "off", why: "key_rejected" };
  if (!status.key_set) return { kind: "off", why: "no_key" };
  if (progress && ["sending", "waiting"].includes(progress.state)) return { kind: "pending" };
  if (progress?.state === "held") return { kind: "held" };
  if (exchange?.outcome === "accepted" && exchange.reply) return { kind: "shown", exchange };
  if (exchange) return { kind: "problem", exchange };
  if (status.gate === "pending") return { kind: "off", why: "gate" };
  return { kind: "pending" };
}

/** Which finding a reference opens in the report: F refs only. */
export function findingFor(exchange: Exchange, ref: string): string | null {
  return ref.startsWith("F") ? (exchange.refs?.[ref] ?? null) : null;
}

/** How the reply's proposed changes fared in the recommendation engine. */
export function verdictCounts(verdicts: Verdict[]): { planned: number; refused: number; unchanged: number } {
  let planned = 0;
  let refused = 0;
  let unchanged = 0;
  for (const v of verdicts) {
    if (["next", "waiting", "disputed"].includes(v.status)) planned++;
    else if (v.status === "unchanged") unchanged++;
    else refused++;
  }
  return { planned, refused, unchanged };
}

/** A recorded packet, laid out for reading; the text that was checked and sent. */
export function prettyPacket(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/** Whether a plan recommendation came from the AI layer (its lead source did). */
export function fromAi(rec: { sources: { origin?: string }[] }): boolean {
  return rec.sources[0]?.origin === "ai";
}

/** One answered chat turn, as `ai.chat_history` returns it. */
export interface ChatTurn {
  exchange_id: string;
  question: string;
  answer: string;
  refs: string[];
  ref_targets: Record<string, string>;
  created: string;
}

/** What preparing a chat turn came to: the exact text for review, or a refusal. */
export type Prepared =
  | { state: "review"; packet_id: string; packet_json: string }
  | { state: "refused"; by: string; reasons: { path: string; rule: string }[] }
  | { state: "blocked" };

export type ChatSent =
  | { state: "done"; exchange: Exchange }
  | { state: "refused"; by: string; reasons: { path: string; rule: string }[] }
  | { state: "capped" | "blocked" };

export async function aiChatPrepare(flightId: string, question: string): Promise<Prepared> {
  return invoke<Prepared>("ai_chat_prepare", { flightId, question });
}

export async function aiChatSend(packetId: string): Promise<ChatSent> {
  return invoke<ChatSent>("ai_chat_send", { packetId });
}

export async function aiChatCancel(packetId: string): Promise<void> {
  return invoke("ai_chat_cancel", { packetId });
}

export const QUESTION_MAX = 500;
