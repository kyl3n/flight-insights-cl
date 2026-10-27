// The guided platform setup: types for the schema the engine serves from the
// knowledge pack, and the pure helpers the wizard uses.

import type { Locale, MessageKey } from "./i18n";

export type Text = Record<Locale, string>;
export type FieldType = "text" | "number" | "integer" | "boolean" | "choice" | "source";

export interface SetupField {
  id: string;
  label: Text;
  type: FieldType;
  required: boolean;
  why?: Text;
  prevents?: Text;
  detect?: string;
  unit?: string;
  choices?: string[];
  /** Words for a choice whose stored value is a code; others show as they are. */
  choice_labels?: Record<string, Text>;
}

export interface SetupStep {
  id: string;
  title: Text;
  about: Text;
  fields: SetupField[];
}

export interface SetupSchema {
  version: number;
  steps: SetupStep[];
}

export interface Answer {
  value: string | number | boolean;
  source: string; // "entered", "detected", or "detected: <what it was read from>"
}

export type Spec = Record<string, Answer>;
export type StepState = "done" | "started" | "not started" | "optional";

export interface Gap {
  step: string;
  field: string;
  label: string;
  prevents: string | null;
}

export interface PlatformView {
  id: string;
  name: string;
  vehicle: "copter" | "plane";
  version: number;
  reference_build: { id: string; version: number };
  spec: Spec;
  completeness: { percent: number; ready: boolean; steps: Record<string, StepState>; gaps: Gap[] };
  airframes: number;
}

export interface AirframeView {
  id: string;
  name: string;
  platform: { id: string; name: string; vehicle: string };
  build: { id: string; number: number };
  spec: Spec;
  differs_from_reference: string[];
}

export function text(t: Text | undefined, locale: Locale): string {
  return t ? (t[locale] ?? t.en) : "";
}

/** The step list's second line: what is left to do. */
export function stepSummary(
  step: SetupStep,
  state: StepState | undefined,
  spec: Spec,
): { key: MessageKey; count?: number } {
  if (state === "done") return { key: "setup.step.done" };
  if (state === "optional") return { key: "setup.step.optional" };
  const left = step.fields.filter((f) => f.required && spec[f.id] === undefined).length;
  if (state === "started" && left > 0) return { key: "setup.step.left", count: left };
  return { key: "setup.step.notStarted" };
}

export type Parsed = { ok: true; value: Answer["value"] | null } | { ok: false; error: MessageKey };

/** What the person typed, as the value the engine stores; an empty box clears it. */
export function parseInput(field: SetupField, raw: string | boolean): Parsed {
  if (field.type === "boolean") return { ok: true, value: Boolean(raw) };
  const s = String(raw).trim();
  if (s === "") return { ok: true, value: null };
  switch (field.type) {
    case "number": {
      const n = Number(s.replace(",", "."));
      return Number.isFinite(n) ? { ok: true, value: n } : { ok: false, error: "setup.error.number" };
    }
    case "integer": {
      const n = Number(s);
      return Number.isInteger(n) ? { ok: true, value: n } : { ok: false, error: "setup.error.integer" };
    }
    case "choice":
      return field.choices?.includes(s) ? { ok: true, value: s } : { ok: false, error: "setup.error.choice" };
    default:
      return { ok: true, value: s };
  }
}

/** The box's text for an answer. */
export function display(answer: Answer | undefined): string {
  if (answer === undefined || typeof answer.value === "boolean") return "";
  return String(answer.value);
}

export function isDetected(answer: Answer | undefined): boolean {
  return answer?.source.startsWith("detected") ?? false;
}

/** How a choice reads in the reader's language. */
export function choiceLabel(field: SetupField, choice: string, locale: Locale): string {
  return field.choice_labels?.[choice]?.[locale] ?? choice;
}

