// Ground station profiles: types for what the engine serves, and the pure
// helpers the Stations view uses to turn what people typed into a save.

import type { MessageKey } from "./i18n";
import { display, parseInput, type Answer, type SetupField, type SetupStep, type Spec } from "./setup";

export interface StationStep extends SetupStep {
  repeat?: boolean; // radios and antennas: a list of entries
}

export interface StationSchema {
  version: number;
  steps: StationStep[];
}

export type Profile = Record<string, Spec | Spec[]>;
export type ChangeReason = "failure" | "upgrade" | "test";
export const CHANGE_REASONS: ChangeReason[] = ["upgrade", "failure", "test"];

export interface StationVersion {
  id: string;
  number: number;
  reason_kind: string | null;
  reason: string | null;
  effective_date: string | null;
}

export interface StationView {
  id: string;
  name: string;
  version: number;
  current: StationVersion & {
    version: number;
    in_use: boolean; // a flight or file refers to it: changes make a new version
    computer_known: boolean;
    profile: Profile;
  };
  history: StationVersion[];
  this_computer?: boolean;
}

/** One entry of a list part as the boxes show it. */
export type Row = Record<string, string | boolean>;

export function specOf(profile: Profile, part: string): Spec {
  const p = profile[part];
  return p && !Array.isArray(p) ? p : {};
}

export function entriesOf(profile: Profile, part: string): Spec[] {
  const p = profile[part];
  return Array.isArray(p) ? p : [];
}

export function rowOf(fields: SetupField[], spec: Spec): Row {
  return Object.fromEntries(
    fields.map((f) => [f.id, f.type === "boolean" ? spec[f.id]?.value === true : display(spec[f.id])]),
  );
}

type Value = Answer["value"];

/** The answers typed into one part's boxes; untouched boxes are left out. */
export function parsePart(
  fields: SetupField[],
  drafts: Row,
): { values: Record<string, Value | null>; errors: Record<string, MessageKey> } {
  const values: Record<string, Value | null> = {};
  const errors: Record<string, MessageKey> = {};
  for (const f of fields) {
    if (!(f.id in drafts)) continue;
    const parsed = parseInput(f, drafts[f.id] ?? "");
    if (parsed.ok) values[f.id] = parsed.value;
    else errors[f.id] = parsed.error;
  }
  return { values, errors };
}

/** A list part's entries as the engine stores them; errors keyed "index/field".
 * An entry left wholly empty is dropped; a filled one needs its required fields. */
export function parseRows(
  fields: SetupField[],
  rows: Row[],
): { entries: Record<string, Value>[]; errors: Record<string, MessageKey> } {
  const entries: Record<string, Value>[] = [];
  const errors: Record<string, MessageKey> = {};
  rows.forEach((row, i) => {
    const entry: Record<string, Value> = {};
    for (const f of fields) {
      const parsed = parseInput(f, row[f.id] ?? "");
      if (!parsed.ok) errors[`${i}/${f.id}`] = parsed.error;
      else if (parsed.value !== null && !(f.type === "boolean" && parsed.value === false)) {
        entry[f.id] = parsed.value;
      }
    }
    if (Object.keys(entry).length === 0) return;
    for (const f of fields) {
      if (f.required && entry[f.id] === undefined) errors[`${i}/${f.id}`] = "stations.error.required";
    }
    entries.push(entry);
  });
  return { entries, errors };
}

/** Whether parsed entries differ from the stored ones. */
export function entriesChanged(entries: Record<string, Value>[], stored: Spec[]): boolean {
  const plain = stored.map((s) => Object.fromEntries(Object.entries(s).map(([k, a]) => [k, a.value])));
  const canon = (xs: Record<string, Value>[]) =>
    JSON.stringify(xs.map((x) => Object.fromEntries(Object.entries(x).filter(([, v]) => v !== false).sort())));
  return canon(entries) !== canon(plain);
}

/** Saves edit the version still being set up; once it has flown, a change needs a reason. */
export function needsReason(view: StationView): boolean {
  return view.current.in_use;
}

/** The history line's reason, in the person's language where it is one of the kinds. */
export function reasonKey(kind: string | null): MessageKey | null {
  switch (kind) {
    case "setup":
      return "stations.reason.setup";
    case "failure":
      return "stations.reason.failure";
    case "upgrade":
      return "stations.reason.upgrade";
    case "test":
      return "stations.reason.test";
    case "detected":
      return "stations.reason.detected";
    default:
      return null;
  }
}
