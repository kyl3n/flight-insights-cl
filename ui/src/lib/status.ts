// One status language everywhere: shape, word and colour together, so a status
// reads in sunlight and for colour-blind users (spec: User interface, Layout).

import type { MessageKey } from "./i18n";

export type Status = "ok" | "caution" | "out" | "info";

export interface StatusLook {
  glyph: string;
  label: MessageKey;
  tone: string; // a CSS custom property
}

export const STATUS: Record<Status, StatusLook> = {
  ok: { glyph: "●", label: "status.ok", tone: "--ok" },
  caution: { glyph: "▲", label: "status.caution", tone: "--caution" },
  out: { glyph: "■", label: "status.out", tone: "--out" },
  info: { glyph: "◆", label: "status.info", tone: "--info" },
};

/** Most urgent first. */
export const URGENCY: Status[] = ["out", "caution", "info", "ok"];

export function byUrgency<T>(items: T[], status: (item: T) => Status): T[] {
  return [...items].sort((a, b) => URGENCY.indexOf(status(a)) - URGENCY.indexOf(status(b)));
}
