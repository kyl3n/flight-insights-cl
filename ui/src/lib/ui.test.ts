import { describe, expect, it } from "vitest";
import { CATALOGS, LOCALES, translate } from "./i18n";
import { inDesktopApp, shellStatus } from "./shell";
import { STATUS, URGENCY, byUrgency, type Status } from "./status";
import { applyTheme, loadTheme, saveTheme, type ThemeStorage } from "./theme";

describe("i18n", () => {
  it("has every message in every language, none empty", () => {
    const keys = Object.keys(CATALOGS.en).sort();
    for (const locale of LOCALES) {
      expect(Object.keys(CATALOGS[locale]).sort()).toEqual(keys);
      for (const key of keys) {
        expect(CATALOGS[locale][key as keyof typeof CATALOGS.en].trim()).not.toBe("");
      }
    }
  });

  it("keeps the same placeholders in every language", () => {
    const names = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const key of Object.keys(CATALOGS.en) as (keyof typeof CATALOGS.en)[]) {
      for (const locale of LOCALES) {
        expect(names(CATALOGS[locale][key]), `${locale} ${key}`).toEqual(names(CATALOGS.en[key]));
      }
    }
  });

  it("fills placeholders", () => {
    const text = translate("en", "fleet.summary", { platforms: 2, airframes: 9, attention: 4 });
    expect(text).toBe("2 platforms · 9 airframes · 4 need attention");
    expect(translate("uk", "fleet.summary", { platforms: 2, airframes: 9, attention: 4 })).toContain("9");
  });
});

describe("status language", () => {
  it("pairs a distinct shape and word with every status", () => {
    const glyphs = Object.values(STATUS).map((s) => s.glyph);
    expect(new Set(glyphs).size).toBe(glyphs.length);
    expect(STATUS.ok.glyph).toBe("●");
    expect(STATUS.caution.glyph).toBe("▲");
    expect(STATUS.out.glyph).toBe("■");
    for (const s of Object.values(STATUS)) expect(CATALOGS.en[s.label]).toBeTruthy();
  });

  it("orders the most urgent first", () => {
    const items: Status[] = ["ok", "info", "out", "caution", "ok"];
    expect(byUrgency(items, (s) => s)).toEqual(["out", "caution", "info", "ok", "ok"]);
    expect(URGENCY[0]).toBe("out");
  });
});

describe("theme", () => {
  const memory = (): ThemeStorage & { data: Map<string, string> } => {
    const data = new Map<string, string>();
    return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
  };

  it("is dark by default and remembers the choice", () => {
    const store = memory();
    expect(loadTheme(store)).toBe("dark");
    saveTheme(store, "light");
    expect(loadTheme(store)).toBe("light");
    const root = { dataset: {} as Record<string, string | undefined> };
    applyTheme(root, "light");
    expect(root.dataset.theme).toBe("light");
  });

  it("still works when storage is blocked", () => {
    const blocked: ThemeStorage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadTheme(blocked)).toBe("dark");
    expect(() => saveTheme(blocked, "light")).not.toThrow();
    expect(loadTheme(undefined)).toBe("dark");
  });
});

describe("shell bridge", () => {
  it("knows when it runs outside the desktop app", async () => {
    expect(inDesktopApp({})).toBe(false);
    expect(inDesktopApp({ __TAURI_INTERNALS__: {} })).toBe(true);
    const status = await shellStatus();
    expect(status.engine).toBe("browser");
    expect(status.laptop).toBeNull();
  });
});
