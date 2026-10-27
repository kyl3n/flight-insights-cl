// The import dialog: types for what the engine's import.scan returns, and the pure
// helpers that turn a person's choices into import.commit decisions.

import type { MessageKey } from "./i18n";
import type { PendingFile } from "./shell";

export type ProposalStatus = "ready" | "duplicate" | "decision" | "no match" | "unpaired" | "error";
export type ChangeKind = "failure" | "upgrade" | "test" | "detected";

export interface HardwareChange {
  part: string;
  before: string | null;
  after: string | null;
  evidence: string;
}

export interface Proposal {
  log: string;
  status: ProposalStatus;
  airframe_id: string | null;
  airframe_name: string | null;
  confidence: "high" | "medium" | null;
  how: "board" | "parameters" | "flight" | "none";
  score: number | null;
  candidates: { airframe_id: string; name: string; score: number }[];
  changes: HardwareChange[];
  note: string | null;
}

export interface ScannedFile {
  id: string;
  name: string;
  path: string;
  kind: "dataflash" | "tlog" | "param" | "params" | null;
  size: number;
  error: string | null;
  vehicle: string | null;
  firmware: string | null;
  board: string | null;
  flights: {
    number: number;
    key: string;
    duration_s: number;
    boot_utc_us: number | null; // UTC at the boot's log time 0
    arm_us: number; // log time of arming
    known: boolean;
  }[];
  boots: number;
  param_writes: number;
  proposal: Proposal;
}

export interface ScanResult {
  batch: string;
  files: ScannedFile[];
}

export interface CommitResult {
  log: string;
  status: "imported" | "held" | "linked" | "skipped" | "duplicate" | "error";
  flights: string[];
  message: string | null;
}

/** What a person chose for one file. */
export type Choice =
  | { kind: "airframe"; airframe_id: string }
  | { kind: "new"; platform_id: string; name: string }
  | { kind: "hold" }
  | { kind: "skip" }
  | { kind: "import" } // tlogs and parameter files: store them
  | { kind: "undecided" };

/** The other part categories the "anything else changed?" checklist asks about. */
export const PARTS = [
  "motors",
  "propellers",
  "escs",
  "batteries",
  "gnss",
  "payload",
  "wiring",
  "links",
] as const;
export type Part = (typeof PARTS)[number];

/** The stable name a part is recorded under in the shared build history, whatever the UI language. */
export const partKey = (p: Part): string => p;

export interface LogChoices {
  choice: Choice;
  confirmed: Record<string, boolean>; // detected change part -> confirmed
  other: Part[]; // ticked part categories
  otherReason: ChangeKind;
  packs: string; // "B-12, B-14"
  payload: string;
  takeoffKg: string; // as typed; see weightKg
}

export function initialChoice(file: ScannedFile): Choice {
  const p = file.proposal;
  if (p.status === "error" || p.status === "duplicate") return { kind: "skip" };
  if (file.kind !== "dataflash") return { kind: "import" };
  if (p.status === "ready" && p.airframe_id) return { kind: "airframe", airframe_id: p.airframe_id };
  if (p.status === "decision") return { kind: "undecided" };
  return { kind: "hold" };
}

export function initialChoices(file: ScannedFile): LogChoices {
  return {
    choice: initialChoice(file),
    confirmed: Object.fromEntries(file.proposal.changes.map((c) => [c.part, true])),
    other: [],
    otherReason: "upgrade",
    packs: "",
    payload: "",
    takeoffKg: "",
  };
}

/** Detected changes were found against the proposed airframe, so they belong to it alone:
 * a log assigned to another airframe, or to a new one, does not carry them. */
export function detectedApplies(file: ScannedFile, choice: Choice): boolean {
  return choice.kind === "airframe" && file.proposal.airframe_id !== null && choice.airframe_id === file.proposal.airframe_id;
}

/** Files still waiting for a decision; the import waits for them. */
export function undecided(files: ScannedFile[], choices: Record<string, LogChoices>): ScannedFile[] {
  return files.filter((f) => {
    const c = choices[f.id]?.choice;
    return c?.kind === "undecided" || (c?.kind === "new" && !c.name.trim());
  });
}

export const MAX_TAKEOFF_KG = 1000; // the engine refuses more (rules/payload.json)

/** A take-off weight as typed, in kilograms: blank is none, and a decimal comma reads
 * as a point. Anything else, or a weight outside 0 to 1000 kg, is "invalid". */
export function weightKg(text: string): number | null | "invalid" {
  const s = text.trim().replace(",", ".");
  if (!s) return null;
  if (!/^\d+(\.\d+)?$/.test(s)) return "invalid";
  const v = Number(s);
  return v > 0 && v <= MAX_TAKEOFF_KG ? v : "invalid";
}

/** Logs being imported whose take-off weight can't be read; the import waits for them. */
export function badWeights(files: ScannedFile[], choices: Record<string, LogChoices>): ScannedFile[] {
  return files.filter((f) => {
    const c = choices[f.id];
    return f.kind === "dataflash" && c !== undefined && c.choice.kind !== "skip" && weightKg(c.takeoffKg) === "invalid";
  });
}

export function packList(text: string): string[] {
  return [...new Set(text.split(/[,;]/).map((s) => s.trim()).filter(Boolean))];
}

export interface Decision {
  log: string;
  action: "import" | "skip";
  airframe_id?: string;
  new_airframe?: { platform_id: string; name: string };
  changes?: { part: string; reason_kind: ChangeKind; reason: string }[];
  packs?: string[];
  payload?: string;
  station_version_id?: string;
  takeoff_kg?: number;
}

/** The import.commit decisions for the batch. Held logs are imported without an airframe.
 * `partName` gives the name a ticked part is recorded under: pass `partKey`, never a translated label. */
export function decisions(
  files: ScannedFile[],
  choices: Record<string, LogChoices>,
  partName: (p: Part) => string,
  stationVersion: string | null,
): Decision[] {
  return files.map((f) => {
    const c = choices[f.id] ?? initialChoices(f);
    if (c.choice.kind === "skip" || c.choice.kind === "undecided") return { log: f.id, action: "skip" };
    const d: Decision = { log: f.id, action: "import" };
    if (stationVersion) d.station_version_id = stationVersion;
    if (c.choice.kind === "airframe") d.airframe_id = c.choice.airframe_id;
    if (c.choice.kind === "new") d.new_airframe = { platform_id: c.choice.platform_id, name: c.choice.name.trim() };
    if (f.kind === "dataflash" && (c.choice.kind === "airframe" || c.choice.kind === "new")) {
      const detected = (detectedApplies(f, c.choice) ? f.proposal.changes : [])
        .filter((x) => c.confirmed[x.part])
        .map((x) => ({ part: x.part, reason_kind: "detected" as ChangeKind, reason: describeChange(x) }));
      const other = c.other.map((p) => ({ part: partName(p), reason_kind: c.otherReason, reason: "" }));
      if (detected.length + other.length) d.changes = [...detected, ...other];
    }
    if (f.kind === "dataflash") {
      const packs = packList(c.packs);
      if (packs.length) d.packs = packs;
      if (c.payload.trim()) d.payload = c.payload.trim();
      const kg = weightKg(c.takeoffKg);
      if (typeof kg === "number") d.takeoff_kg = kg;
    }
    return d;
  });
}

/** Files from watched folders are recorded against the station whose folder they
 * were found in, whatever station is picked for the rest of the batch. */
export function withStations(out: Decision[], files: ScannedFile[], pending: PendingFile[]): Decision[] {
  const station = new Map(pending.map((p) => [p.path, p.station_version_id]));
  return out.map((d) => {
    const path = files.find((f) => f.id === d.log)?.path;
    const found = path === undefined ? undefined : station.get(path);
    return d.action === "import" && found ? { ...d, station_version_id: found } : d;
  });
}

/** Watched files a person chose not to import: set aside so they are not offered again. */
export function declined(files: ScannedFile[], choices: Record<string, LogChoices>, pending: PendingFile[]): string[] {
  const watched = new Set(pending.map((p) => p.path));
  return files
    .filter((f) => watched.has(f.path) && choices[f.id]?.choice.kind === "skip" && f.proposal.status !== "error")
    .map((f) => f.path);
}

export function describeChange(c: HardwareChange): string {
  return `${c.before ?? "none"} → ${c.after ?? "none"}`;
}

/** The status glyph and word for a file (spec: User interface, the status language). */
export function fileStatus(file: ScannedFile, choice: Choice): { glyph: string; key: MessageKey } {
  if (file.proposal.status === "error") return { glyph: "■", key: "import.status.error" };
  if (choice.kind === "undecided") return { glyph: "▲", key: "import.status.decide" };
  if (choice.kind === "skip") return { glyph: "○", key: file.proposal.status === "duplicate" ? "import.status.duplicate" : "import.status.skip" };
  if (choice.kind === "hold") return { glyph: "▲", key: "import.status.hold" };
  return { glyph: "●", key: "import.status.ready" };
}

/** How the airframe was matched, as the table's "How it was matched" column says it. */
export function matchKey(p: Proposal): MessageKey {
  if (p.status === "decision") return "import.match.newBoard";
  switch (p.how) {
    case "board":
      return "import.match.board";
    case "parameters":
      return "import.match.parameters";
    case "flight":
      return "import.match.flight";
    default:
      return p.status === "unpaired" ? "import.match.unpaired" : "import.match.none";
  }
}

/** "18 Oct 14:06 · 18 min": the first flight's take-off, in the computer's time zone. */
export function whenText(file: ScannedFile, locale: string, minutesText: (n: number) => string): string {
  const f = file.flights[0];
  if (!f) return "";
  const minutes = minutesText(Math.max(1, Math.round(f.duration_s / 60)));
  if (f.boot_utc_us === null) return minutes;
  const at = new Date((f.boot_utc_us + f.arm_us) / 1000);
  const date = at.toLocaleDateString(locale, { day: "numeric", month: "short" });
  const time = at.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  return `${date} ${time} · ${minutes}`;
}

export function resultCounts(results: CommitResult[]): Record<CommitResult["status"], number> {
  const out = { imported: 0, held: 0, linked: 0, skipped: 0, duplicate: 0, error: 0 };
  for (const r of results) out[r.status] += 1;
  return out;
}
