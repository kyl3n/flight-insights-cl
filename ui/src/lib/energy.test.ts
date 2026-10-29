import { describe, expect, it } from "vitest";
import { axis, bar, clock, curveChart, shaded, standing, watched, PART_COLOURS, type Breakdown, type OnCurve, type Watched } from "./energy";
import { CATALOGS } from "./i18n";

const breakdown = (parts: [string, number][], cells: number | null = null): Breakdown => ({
  parts: parts.map(([part, wh]) => ({ part, wh })),
  terminal_wh: parts.reduce((s, [, wh]) => s + wh, 0),
  cells_wh: cells,
  split: true,
  split_why: null,
  heat_why: null,
});

describe("the parts of the energy", () => {
  it("lays the parts with energy along one bar in order, adding up to the whole", () => {
    const b = bar(breakdown([["heat", 10], ["level", 40], ["low", 0], ["hover", 50], ["avionics", 0]]));
    expect(b.map((p) => p.part)).toEqual(["hover", "level", "heat"]);
    expect(b.map((p) => p.share)).toEqual([0.5, 0.4, 0.1]);
    expect(b.map((p) => p.from)).toEqual([0, 0.5, 0.9]);
    expect(b[0]!.colour).toBe(PART_COLOURS.hover);
  });

  it("leaves a part that came out below nothing off the bar", () => {
    // between-tiles energy is what is left over; rounding can take it under zero
    expect(bar(breakdown([["hover", 10], ["untiled", -0.01]])).map((p) => p.part)).toEqual(["hover"]);
  });

  it("names every part and phase in both languages", () => {
    for (const part of Object.keys(PART_COLOURS)) {
      for (const lang of ["en", "uk"] as const) {
        expect(CATALOGS[lang][`energy.part.${part}` as keyof (typeof CATALOGS)["en"]]).toBeTruthy();
      }
    }
  });
});

describe("the phases over time", () => {
  it("colours each span by its phase", () => {
    expect(shaded([{ phase: "climb", start_s: 10, end_s: 14 }])).toEqual([{ start: 10, end: 14, colour: PART_COLOURS.climb }]);
  });

  it("writes flight time as minutes and seconds", () => {
    expect(clock(880)).toBe("14:40");
    expect(clock(59.6)).toBe("1:00");
  });
});

describe("against the model", () => {
  const a = { excess_pct: 3.7, interval_pct: [-7.7, 14.7] as [number, number], by_phase_pct: {}, threshold_pct: 8, others: 3 };

  it("is within the airframe's usual spread, or over it", () => {
    expect(standing(a, [])).toBe("ok");
    expect(standing({ ...a, excess_pct: 12 }, [])).toBe("caution");
    expect(standing({ ...a, excess_pct: -12 }, [])).toBe("caution");
  });

  it("is caution whenever the anomaly is raised, and unknown without a judgement", () => {
    expect(standing(a, [{ code: "performance.energy_hover", severity: "caution", params: {} }])).toBe("caution");
    expect(standing(null, [])).toBeNull();
  });
});

describe("the curve chart", () => {
  it("takes round axes from zero speed", () => {
    expect(axis([0, 16], 4, 0)).toEqual({ lo: 0, hi: 20, ticks: [0, 5, 10, 15, 20] });
    const y = axis([3094, 3532]);
    expect(y.lo).toBeLessThanOrEqual(3094);
    expect(y.hi).toBeGreaterThanOrEqual(3532);
    expect(y.ticks[0]).toBe(y.lo);
    expect(y.ticks.at(-1)).toBe(y.hi);
  });

  const onCurve: OnCurve = {
    why: null,
    points: [
      { phase: "hover", airspeed_ms: 0.2, climb_ms: 0, power_w: 3070, seconds: 30, tiles: 15 },
      { phase: "climb", airspeed_ms: 0.2, climb_ms: 2.4, power_w: 3400, seconds: 4, tiles: 2 },
      { phase: "level", airspeed_ms: 12, climb_ms: 0, power_w: 3100, seconds: 22, tiles: 11 },
    ],
    mass_kg: 32.5,
    mass_from: "weighed at import",
    density_ratio: 0.94,
    wind_from: "flight",
    level: "B",
    curve: { airspeed_ms: [0, 8, 16], power_w: [3200, 3000, 3300], low_w: [3100, 2900, 3200], high_w: [3500, 3200, 3600] },
    hover: null,
    against: null,
  };
  const frame = { width: 640, height: 230, left: 56, right: 16, top: 14, bottom: 34 };

  it("draws the model, its band and the hover and level points inside the frame", () => {
    const c = curveChart(onCurve, frame)!;
    expect(c.line.startsWith("M56.0 ")).toBe(true);
    expect(c.band?.endsWith("Z")).toBe(true);
    expect(c.points.map((p) => p.phase)).toEqual(["hover", "level"]); // climbs are off the level curve
    for (const p of c.points) {
      expect(p.x).toBeGreaterThanOrEqual(frame.left);
      expect(p.x).toBeLessThanOrEqual(frame.width - frame.right);
      expect(p.y).toBeGreaterThanOrEqual(frame.top);
      expect(p.y).toBeLessThanOrEqual(frame.height - frame.bottom);
    }
    // more power is higher up
    expect(c.py(3500)).toBeLessThan(c.py(3000));
  });

  it("sets the power axis by the speeds flown, and marks the curve beyond them", () => {
    // a band that widens without bound past the fastest point does not squash the points
    const wide = { ...onCurve.curve!, airspeed_ms: [0, 8, 14, 20], power_w: [3200, 3000, 3200, 3600], low_w: [3100, 2900, 2500, 1000], high_w: [3500, 3200, 3900, 9000] };
    const c = curveChart({ ...onCurve, curve: wide }, frame)!;
    expect(c.y.hi).toBeLessThan(5000);
    expect(c.flown).toBeCloseTo(c.px(12));
  });

  it("draws no band without the bootstrap, and nothing without a model", () => {
    const c = curveChart({ ...onCurve, curve: { ...onCurve.curve!, low_w: null, high_w: null } }, frame)!;
    expect(c.band).toBeNull();
    expect(curveChart({ ...onCurve, curve: null }, frame)).toBeNull();
  });
});

describe("the fast run", () => {
  it("lists what was watched in a fixed order, leaving out what it does not know", () => {
    const rows: Watched[] = ["fast.speed_ms", "fast.lean_share", "fast.vibe_p95", "fast.other"].map((metric) => ({
      metric,
      value: 1,
      zone: "target",
      evidence: "fast:x",
    }));
    expect(watched(rows).map((r) => r.metric)).toEqual(["fast.vibe_p95", "fast.lean_share"]);
  });
});
