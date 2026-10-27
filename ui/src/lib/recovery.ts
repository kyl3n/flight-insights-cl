// The airframe's recovery card, as the engine's airframe.recovery_card returns it, and the
// helpers behind its screen (spec: Navigation and GNSS, "Recovery card"; mock-up
// RecoveryCard): what the aircraft will do and what the operator does for each loss,
// worded by the engine in both languages, to print and keep with the aircraft. Nothing
// here words a line or judges one: the engine marks the alerts.

import { engineCall } from "./shell";
import type { Text } from "./report";

export interface Phrase {
  key: string;
  text: Text;
}

export interface CardSection {
  id: string; // gnss_lost, spoofing, link_lost or radio_lost and telemetry_lost, motor_lost
  title: Text;
  will: Phrase[];
  facts: Phrase[];
  steps: Phrase[];
  alert: boolean; // it ends with no one flying it, or lost: marked in print
}

export interface Measured {
  outages: number; // outages and drills
  hold_s: number | null;
  detection_s: number | null;
  drift_m_per_min: number | null;
}

export interface RecoveryCard {
  airframe: string;
  param_set: string | null;
  title: Text;
  valid: Text | null;
  home_heading: Text;
  will_label: Text;
  fallback_mode: string | null;
  measured: { alone: Measured; on_set: Measured; tolerance_m: number };
  sections: CardSection[];
  gaps: string[]; // the readiness review's findings, by code
  // Whether it gets home with the pilot's control and GNSS both lost, from every mode and
  // whichever goes first, and the link section's lines that say what it does; null with
  // no link sections.
  goal: { met: boolean; section: string; will: Phrase[] } | null;
  footer: Text;
}

export function recoveryCard(airframeId: string): Promise<RecoveryCard> {
  return engineCall<RecoveryCard>("airframe.recovery_card", { airframe_id: airframeId });
}

export type Look = "section" | "alert" | "spoofing";

/** How a section is drawn: suspected spoofing apart, with a dashed edge; an alert in red. */
export function look(s: CardSection): Look {
  if (s.id === "spoofing") return "spoofing";
  return s.alert ? "alert" : "section";
}

/** Sections sit two to a row where they are short. One with long lines, as the link's
 * are, takes the whole row, and spoofing comes last across the whole card. */
export function wide(s: CardSection): boolean {
  return s.id === "spoofing" || s.id === "link_lost" || s.will.length + s.facts.length > 5;
}

/** In print order: the sections, with spoofing after the rest. */
export function ordered(card: RecoveryCard): CardSection[] {
  return [...card.sections.filter((s) => s.id !== "spoofing"), ...card.sections.filter((s) => s.id === "spoofing")];
}

/** Outages and drills the card's numbers rest on, on its own and on another source set. */
export function outages(card: RecoveryCard): number {
  return card.measured.alone.outages + card.measured.on_set.outages;
}

/** The parameter set as short as the card prints it. */
export function shortSet(paramSet: string | null): string | null {
  return paramSet === null ? null : paramSet.slice(0, 8);
}
