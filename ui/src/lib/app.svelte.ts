// Application state shared by every view: language, theme, the scope switcher
// and the shell's status.

import { translate, type Locale, type MessageKey } from "./i18n";
import { BROWSER_STATUS, type ShellStatus } from "./shell";
import type { Theme } from "./theme";

export type View = "fleet" | "flights" | "trends" | "params" | "plan" | "ask" | "imports" | "settings";

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
});

export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  return translate(app.locale, key, vars);
}
