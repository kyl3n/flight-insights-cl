import { describe, expect, it } from "vitest";
import {
  fill,
  findingTitle,
  formatDuration,
  formatValue,
  groupMetrics,
  intervalText,
  scale,
  severityLook,
  sortFindings,
  stageKey,
  zoneLook,
  type FindingRow,
  type MetricInfo,
  type Registry,
} from "./report";
import { CATALOGS } from "./i18n";

const text = (en: string, uk = en) => ({ en, uk });
const VIBE: MetricInfo = {
  name: text("Hover vibration", "Вібрація"),
  unit: "m/s/s",
  direction: "lower",
  definition: text("d"),
  method: text("m"),
  messages: ["VIBE"],
  zones: { target: [[null, 30]], caution: [[30, 60]], out: [[60, null]] },
  source: { kind: "ardupilot", title: "ArduPilot: measuring vibration" },
};
const REG: Registry = {
  version: "kp",
  metrics: { "vibration.p95": VIBE },
  findings: {
    "esc.hot": { title: text("ESC {esc} at {temp} °C", "ESC {esc}: {temp} °C"), action: text("a"), stage: "mechanical" },
  },
};

function finding(code: string, severity: FindingRow["severity"], stage: string): FindingRow {
  return { id: code, analyzer: "x", code, severity, confidence: "high", params: {}, evidence: [], changes: [], stage };
}

describe("report", () => {
  it("fills a finding's title in the chosen language", () => {
    const f = { ...finding("esc.hot", "caution", "mechanical"), params: { esc: 3, temp: 96.04 } };
    expect(findingTitle(REG, f, "en")).toBe("ESC 3 at 96 °C");
    expect(findingTitle(REG, f, "uk")).toBe("ESC 3: 96 °C");
    expect(findingTitle(REG, finding("no.such", "info", "setup"), "en")).toBe("no.such");
    expect(fill("{names} and {missing}", { names: ["A", "B"] }, "en")).toBe("A, B and {missing}");
  });

  it("puts the most urgent findings first, then the tuning order", () => {
    const sorted = sortFindings([
      finding("a", "info", "setup"),
      finding("b", "caution", "filters"),
      finding("c", "caution", "mechanical"),
      finding("d", "safety", "operations"),
    ]);
    expect(sorted.map((f) => f.id)).toEqual(["d", "c", "b", "a"]);
  });

  it("shows severities and zones in the one status language", () => {
    expect(severityLook("safety")).toEqual({ status: "out", label: "severity.safety" });
    expect(zoneLook("target").status).toBe("ok");
    expect(zoneLook(null).label).toBe("zone.none");
    for (const stage of ["mechanical", "rate tune", "operations"]) {
      expect(stageKey(stage) in CATALOGS.en).toBe(true);
    }
  });

  it("formats values with their units as people read them", () => {
    expect(formatValue(18.04, "m/s/s")).toBe("18 m/s/s");
    expect(formatValue(96, "degC")).toBe("96 °C");
    expect(formatValue(12.5, "%")).toBe("12.5%");
    expect(formatValue(0.4123, "")).toBe("0.412");
    expect(formatValue(1234.5, "W")).toBe("1,235 W");
    expect(formatValue(3, "count")).toBe("3");
    expect(formatDuration(1112)).toEqual({ min: 18, s: 32 });
    expect(formatValue(4264, "W", "uk")).toBe("4\u00a0264 Вт");
    // a PWM margin is written in microseconds, not "us"
    expect(formatValue(25, "us")).toBe("25 µs");
    expect(formatValue(25, "us", "uk")).toBe("25 мкс");
    expect(formatValue(13.1, "mOhm", "uk")).toBe("13,1 мОм");
  });

  it("lays the zones and the value out on one scale", () => {
    const s = scale(VIBE, 18);
    expect(s).not.toBeNull();
    expect(s!.bands.map((b) => b.zone)).toEqual(["target", "caution", "out"]);
    expect(s!.bands.at(0)?.from).toBe(0);
    expect(s!.bands.at(-1)?.to).toBe(1);
    expect(s!.at).toBeGreaterThan(0);
    expect(s!.at).toBeLessThan(s!.bands.at(0)?.to ?? 0);
    const words = { below: "below", above: "and above" };
    expect(intervalText([null, 30], "m/s/s", "en", words)).toBe("below 30 m/s/s");
    expect(intervalText([30, 60], "m/s/s", "en", words)).toBe("30–60 m/s/s");
    expect(intervalText([60, null], "m/s/s", "en", words)).toBe("60 m/s/s and above");
  });

  it("groups values by subsystem and drops ones the registry does not know", () => {
    const groups = groupMetrics(
      [
        { metric: "vibration.p95", segment: "hover", value: 18, spread: {}, zone: "target", evidence: "e" },
        { metric: "gone.metric", segment: "flight", value: 1, spread: {}, zone: null, evidence: "e" },
      ],
      REG,
    );
    expect(groups.map(([k, v]) => [k, v.length])).toEqual([["vibration", 1]]);
  });
});

describe("angles", () => {
  it("writes degrees as a sign with no space", () => {
    expect(formatValue(11.2, "deg", "en")).toBe("11.2°");
    expect(formatValue(11.2, "deg", "uk")).toBe("11,2°");
  });
});
