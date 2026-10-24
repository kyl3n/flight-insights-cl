// Sync and the fleet as the interface sees them (spec: Shared storage and sync). The
// shell signs in to Google, holds every key and moves every object; this side shows
// where things stand and passes on a person's choices. It never sees a token or a key:
// only whether there is one, fingerprints to compare, and the recovery words once.

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { MessageKey } from "./i18n";
import { engineCall, inDesktopApp } from "./shell";
import type { Status } from "./status";

/** The worker's word on the last round. "" before its first round. */
export type WorkerStateName = "" | "off" | "idle" | "syncing" | "offline" | "sign_in" | "revoked" | "error";

export interface WorkerState {
  state: WorkerStateName;
  why: string | null;
  last_sync_ms: number | null;
  uploaded: number;
  applied: number;
  waiting: number;
  quarantined: number;
  sources_moved: number;
  /** Objects the drive lists for a laptop that are not here yet: [laptop, sequence]. */
  gaps: [string, number][];
}

export interface Member {
  id: string;
  name: string;
  fingerprint: string;
  revoked: boolean;
  me: boolean;
}

/** What the engine knows: from `sync.status`. */
export interface EngineSync {
  waiting_to_send: number;
  laptops: { laptop: string; latest: number; objects: number; gaps: number[] }[];
  quarantine: { object_key: string; laptop: string; sequence: number | null; reason: string; at: string }[];
  conflicts: number;
}

export interface SyncStatus {
  /** Whether this build can sign in to Google at all. */
  client: boolean;
  signed_in: boolean;
  drive: { id: string; name: string | null } | null;
  fleet: { fingerprint: string; members: Member[] } | null;
  worker: WorkerState;
  engine: EngineSync | null;
}

export interface Conflict {
  id: number;
  tbl: string;
  rid: string;
  field: string;
  kept: unknown;
  kept_hlc: string;
  other: unknown;
  other_hlc: string;
  at_hlc: string;
  resolved: boolean;
}

export const EMPTY_WORKER: WorkerState = {
  state: "",
  why: null,
  last_sync_ms: null,
  uploaded: 0,
  applied: 0,
  waiting: 0,
  quarantined: 0,
  sources_moved: 0,
  gaps: [],
};

// --- the shell's commands -------------------------------------------------------------

export async function syncStatus(): Promise<SyncStatus | null> {
  if (!inDesktopApp()) return null;
  return invoke<SyncStatus>("sync_status");
}

export async function onSyncStatus(handler: (w: WorkerState) => void): Promise<UnlistenFn> {
  if (!inDesktopApp()) return () => {};
  return listen<WorkerState>("sync-status", (e) => handler(e.payload));
}

export const googleSignIn = (): Promise<void> => invoke("google_sign_in");
export const googleSignOut = (): Promise<void> => invoke("google_sign_out");
export const googleDrives = (): Promise<{ id: string; name: string }[]> => invoke("google_drives");
export const chooseDrive = (id: string, name: string): Promise<void> => invoke("sync_choose_drive", { id, name });
export const syncNow = (): Promise<void> => invoke("sync_now");

/** A new fleet. The recovery words come back this once and are never shown again. */
export const fleetCreate = (name: string): Promise<{ words: string; fingerprint: string }> =>
  invoke("fleet_create", { name });
export const fleetCode = (name: string): Promise<{ code: string; fingerprint: string }> =>
  invoke("fleet_code", { name });
export const fleetFind = (): Promise<string | null> => invoke("fleet_find");
export const fleetJoin = (fingerprint: string): Promise<void> => invoke("fleet_join", { fingerprint });
export const fleetPreview = (code: string): Promise<{ name: string; fingerprint: string }> =>
  invoke("fleet_preview", { code });
export const fleetApprove = (code: string): Promise<void> => invoke("fleet_approve", { code });
export const fleetRemove = (laptop: string): Promise<void> => invoke("fleet_remove", { laptop });
export const fleetRecover = (words: string, name: string): Promise<string> =>
  invoke("fleet_recover", { words, name });

export const conflicts = (): Promise<Conflict[]> => engineCall<Conflict[]>("sync.conflicts");
/** "kept" leaves the value that won; "other" sets the other one as an ordinary edit. */
export const resolveConflict = (id: number, keep: "kept" | "other"): Promise<object> =>
  engineCall("sync.resolve", { id, keep });

// --- pure helpers ---------------------------------------------------------------------

/** The header's sync pill: shape and word for where sync stands. */
export function syncPill(s: WorkerState | null): { status: Status; label: MessageKey } {
  if (!s) return { status: "info", label: "sync.state.browser" };
  switch (s.state) {
    case "idle":
      if (s.quarantined > 0) return { status: "caution", label: "sync.state.quarantined" };
      if (s.gaps.length > 0) return { status: "caution", label: "sync.state.gaps" };
      return { status: "ok", label: "sync.state.idle" };
    case "syncing":
      return { status: "info", label: "sync.state.syncing" };
    case "offline":
      return { status: "caution", label: "sync.state.offline" };
    case "sign_in":
      return { status: "caution", label: "sync.state.sign_in" };
    case "revoked":
      return { status: "out", label: "sync.state.revoked" };
    case "error":
      return { status: "out", label: "sync.state.error" };
    case "off":
      return { status: "info", label: "sync.state.off" };
    default:
      return { status: "info", label: "sync.state.starting" };
  }
}

/** How long ago, in the largest whole unit: for "last sync 3 min ago". */
export function ago(thenMs: number | null, nowMs: number): { key: MessageKey; n: number } {
  if (thenMs === null) return { key: "sync.ago.never", n: 0 };
  const s = Math.max(0, Math.floor((nowMs - thenMs) / 1000));
  if (s < 60) return { key: "sync.ago.now", n: 0 };
  if (s < 3600) return { key: "sync.ago.min", n: Math.floor(s / 60) };
  if (s < 86400) return { key: "sync.ago.hour", n: Math.floor(s / 3600) };
  return { key: "sync.ago.day", n: Math.floor(s / 86400) };
}

/** A fingerprint as typed (any case, spaces or dashes) in the shown form, or null when
 * it is not 12 hex digits. */
export function normalizeFingerprint(text: string): string | null {
  const hex = text.toUpperCase().replace(/[\s-]/g, "");
  if (!/^[0-9A-F]{12}$/.test(hex)) return null;
  return `${hex.slice(0, 4)}-${hex.slice(4, 8)}-${hex.slice(8)}`;
}

/** The words split as the shell joins them. */
export function splitWords(words: string): string[] {
  return words.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

/** Three distinct word positions (0-based, in order) to type back, proving the words
 * were written down. `random` returns [0, 1). */
export function pickChecks(count: number, random: () => number = Math.random): number[] {
  const picked = new Set<number>();
  while (picked.size < Math.min(3, count)) picked.add(Math.floor(random() * count));
  return [...picked].sort((a, b) => a - b);
}

/** Whether every typed word matches the word at its position. */
export function checksMatch(words: string[], checks: number[], typed: string[]): boolean {
  return checks.every((i, k) => (typed[k] ?? "").trim().toLowerCase() === words[i]);
}

/** A laptop's name from the registry, or its ID shortened when it is not a member. */
export function laptopName(id: string, members: Member[]): string {
  return members.find((m) => m.id === id)?.name ?? `LT-${id.slice(0, 6).toUpperCase()}`;
}

/** A conflicting value as text, for both sides to be read at a glance. */
export function showValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** Gaps by laptop, for the panel: the worker's (from the drive's listing) and the
 * engine's (sequences skipped in what was applied), merged. */
export function gapsByLaptop(worker: WorkerState, engine: EngineSync | null): Map<string, number[]> {
  const out = new Map<string, Set<number>>();
  const add = (laptop: string, seq: number) => {
    if (!out.has(laptop)) out.set(laptop, new Set());
    out.get(laptop)!.add(seq);
  };
  for (const [laptop, seq] of worker.gaps) add(laptop, seq);
  for (const l of engine?.laptops ?? []) for (const seq of l.gaps) add(l.laptop, seq);
  return new Map([...out].map(([k, v]) => [k, [...v].sort((a, b) => a - b)]));
}
