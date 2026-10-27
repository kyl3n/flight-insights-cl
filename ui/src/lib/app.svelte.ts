// Application state shared by every view: language, theme, the scope switcher
// and the shell's status.

import type { AiStatus } from "./ai";
import type { WorkerState } from "./sync";
import { translate, type Locale, type MessageKey } from "./i18n";
import { BROWSER_STATUS, type PendingFile, type ShellStatus } from "./shell";
import type { Theme } from "./theme";

export type View =
  | "fleet"
  | "flights"
  | "trends"
  | "params"
  | "plan"
  | "ask"
  | "stations"
  | "sync"
  | "imports"
  | "settings";

export interface Scope {
  platform: string | null; // null: all
  airframe: string | null;
  flight: string | null;
}

export const app = $state({
  locale: "en" as Locale,
  theme: "dark" as Theme,
  view: "fleet" as View,
  scope: { platform: null, airframe: null, flight: null } as Scope,
  shell: BROWSER_STATUS as ShellStatus,
  setupPlatform: null as string | null, // the platform open in the setup wizard
  importOpen: false, // the import dialog
  pending: [] as PendingFile[], // new files found in watched folders
  trendCard: null as string | null, // the metric@segment the Trends view opens on
  trendsTab: "metrics" as "metrics" | "history", // the Trends view's metrics or parameter history
  ai: null as AiStatus | null, // the AI layer's status; null outside the desktop app
  sync: null as WorkerState | null, // the sync worker's last round; null outside the desktop app
});

export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  return translate(app.locale, key, vars);
}
