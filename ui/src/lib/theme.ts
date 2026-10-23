// Dark by default, for spectrograms; a high-contrast light theme for field laptops
// in sunlight. The choice is remembered per laptop when storage allows it.

export type Theme = "dark" | "light";
const KEY = "fi.theme";

export interface ThemeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function loadTheme(storage: ThemeStorage | undefined): Theme {
  try {
    return storage?.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark"; // storage blocked: fall back to the default
  }
}

export function saveTheme(storage: ThemeStorage | undefined, theme: Theme): void {
  try {
    storage?.setItem(KEY, theme);
  } catch {
    // not remembered, still applied
  }
}

export function applyTheme(root: { dataset: Record<string, string | undefined> }, theme: Theme) {
  root.dataset.theme = theme;
}
