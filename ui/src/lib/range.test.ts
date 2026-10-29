import { describe, expect, it } from "vitest";
import { againstSeaLevel, at, band, overTimeSeries, earlyEnds, flownSpan, interval, least, measuredAt, minutesGrid, packShares, plot, polar, split, used, weightChart, weights, whatIfShares, type Figures, type Inputs } from "./range";

const inputs = (over: Partial<Inputs> = {}): Inputs => ({
  mass_kg: 32.5,
  mass_from: "the setup's typical take-off",
  density: 1.156,
  air: "usual",
  empty_kg: 30,
  max_kg: 45,
  flown_kg: [40.5, 32.5, 32.5],
  top_ms: 12,
  reserve_share: 0.2,
  trip: "out_and_back",
  wind_ms: 5,
  ...over,
});

describe("the weights to pick from", () => {
  it("offers the engine's pick, then empty, each weight flown and the most it may carry, each once", () => {
    expect(weights(inputs()).map((w) => [w.role, w.kg])).toEqual([
      ["typical", null],
      ["empty", 30],
      ["flown", 40.5],
      ["max", 45],
    ]);
  });

  it("offers only the engine's pick before the figures arrive", () => {
    expect(weights(undefined)).toEqual([{ kg: null, role: "typical" }]);
  });

  it("knows the span flown, and whether a weight is inside it", () => {
    const span = flownSpan(inputs());
    expect(span).toEqual([32.5, 40.5]);
    expect(measuredAt(36, span)).toBe(true);
    expect(measuredAt(45, span)).toBe(false);
    expect(measuredAt(null, span)).toBe(false);
    expect(flownSpan(inputs({ flown_kg: [] }))).toBeNull();
  });
});

describe("figures against weight", () => {
  const row = (kg: number, km: number): Figures => ({
    mass_kg: kg,
    hover_s: 1000,
    endurance_s: 1100,
    endurance_ms: 8,
    range_m: km * 1000,
    range_ms: 12,
    radius_m: {},
    measured: true,
    efficiency: { figure_of_merit: 0.6, grams_per_watt: 8, wh_per_km: 80, wh_per_kg_km: 2.4, cost_of_transport: 0.8 },
  });
  const frame = { width: 900, height: 220, left: 56, right: 20, top: 16, bottom: 34 };

  it("draws the weights in order, heavier to the right and longer range higher up", () => {
    const c = weightChart([row(40, 12), row(30, 16), row(35, 14)], (r) => r.range_m / 1000, frame)!;
    expect(c.points.map((p) => p.kg)).toEqual([30, 35, 40]);
    expect(c.points[0]!.x).toBeLessThan(c.points[2]!.x);
    expect(c.points[0]!.y).toBeLessThan(c.points[2]!.y);
    expect(c.y.lo).toBe(0);
    expect(c.line.startsWith("M")).toBe(true);
  });

  it("draws nothing from a single weight", () => {
    expect(weightChart([row(30, 16)], (r) => r.range_m, frame)).toBeNull();
  });

  it("writes an interval as low to high, or nothing without one", () => {
    expect(interval({ low: 1200, median: 1500, high: 1800 }, (s) => String(s / 60))).toBe("20–30");
    expect(interval(undefined, String)).toBeNull();
  });
});

describe("what ends the figures", () => {
  it("names each failsafe that ends a figure before the reserve, once", () => {
    expect(earlyEnds({ hover: "capacity_failsafe", endurance: "capacity_failsafe", range: "voltage_failsafe" })).toEqual([
      "capacity_failsafe",
      "voltage_failsafe",
    ]);
    expect(earlyEnds({ hover: "reserve", endurance: "cutoff", range: "reserve" })).toEqual([]);
    expect(earlyEnds(undefined)).toEqual([]);
  });
});

describe("the speed charts", () => {
  const frame = { width: 600, height: 200, left: 50, right: 10, top: 10, bottom: 30 };
  const xs = [0, 2, 4, 6, 8, 10];

  it("draws a line solid up to the speeds flown and dashed beyond, joined where it crosses", () => {
    const p = plot(xs, [1, 5], frame);
    const ln = split(xs, [3, 2.5, 2, 2.2, 2.6, 3.2], 5, p);
    expect(ln.solid.match(/M/g)).toHaveLength(1);
    expect(ln.solid.match(/L/g)).toHaveLength(2); // 0, 2, 4
    expect(ln.beyond.startsWith(`M${p.px(4).toFixed(1)}`)).toBe(true); // from the last flown point
  });

  it("breaks a line where a value is missing, as into a headwind faster than the speed", () => {
    const p = plot(xs, [0, 400], frame);
    const ln = split(xs, [null, null, null, 300, 200, 150], 10, p);
    expect(ln.solid.startsWith(`M${p.px(6).toFixed(1)}`)).toBe(true);
    expect(ln.beyond).toBe("");
  });

  it("reads a curve between its points and finds its least within the speeds flown", () => {
    const ys = [300, 200, 120, 100, 110, 90];
    expect(at(xs, ys, 5)).toBe(110);
    expect(at(xs, [null, 1], 1)).toBeNull();
    expect(least(xs, ys, 8)).toEqual({ v: 6, y: 100 }); // the 90 at 10 is beyond them
  });

  it("closes a band from the high line out and the low line back", () => {
    const p = plot(xs, [0, 10], frame);
    const b = band([0, 10], [1, 2], [3, 4], p);
    expect(b.startsWith("M")).toBe(true);
    expect(b.endsWith("Z")).toBe(true);
    expect(b.match(/L/g)).toHaveLength(3);
  });
});

describe("wind and the build-up", () => {
  it("draws the polar with out into the wind at the top", () => {
    // the engine's 0 is out with the wind behind; its 180, out into the wind
    const d = polar([[0, 1000], [90, 1500], [180, 1000], [270, 1500]], 100, 100, 50, 1500);
    const pts = d.replace(/[MZ]/g, "").split(" L").map((p) => p.trim().split(" ").map(Number));
    expect(pts[2]![1]!).toBeLessThan(100); // 180: up
    expect(pts[0]![1]!).toBeGreaterThan(100); // 0: down
    expect(pts[1]![0]!).toBeCloseTo(50); // 90: the full radius across, on the left
    expect(polar([], 0, 0, 1, 1)).toBe("");
  });

  it("gives each check's share of its limit", () => {
    expect(used({ name: "lean", value: 20, limit: 80, holds: true, how: "model" })).toBe(0.25);
    expect(used({ name: "current", value: 5, limit: 0, holds: false, how: "model" })).toBe(1);
  });
});

describe("batteries and what-ifs", () => {
  it("gives each pack's capacity as a share of rated and resistance against its first flights", () => {
    const got = packShares({
      code: "P1",
      rated_mah: 30000,
      flights: [
        { capacity_mah: 29400, resistance_mohm: 20 },
        { capacity_mah: null, resistance_mohm: 22 },
        { capacity_mah: 27000, resistance_mohm: 30 },
        { capacity_mah: 24000, resistance_mohm: 44 },
      ],
    });
    expect(got.capacity).toEqual([0.98, null, 0.9, 0.8]);
    // new is the median of the first two of four: 21 mOhm
    expect(got.resistance.map((r) => Math.round(r * 100) / 100)).toEqual([0.95, 1.05, 1.43, 2.1]);
  });

  it("leaves capacity out without a rated capacity", () => {
    const got = packShares({ code: "P2", rated_mah: null, flights: [{ capacity_mah: 5000, resistance_mohm: 10 }] });
    expect(got.capacity).toEqual([null]);
    expect(got.resistance).toEqual([1]);
  });

  it("gives a what-if's gains as shares of the figures", () => {
    const f = { range_m: 10000, endurance_s: 1200 } as Figures;
    const w = { lever: "mass" as const, change: -1, hover_s: 50, endurance_s: 60, range_m: 500, radius_m: 250, share: -0.05, basis: "measured" as const };
    expect(whatIfShares(w, f)).toEqual({ range: 0.05, endurance: 0.05 });
  });
});

describe("the air and the data behind", () => {
  it("gives each figure's change against sea level", () => {
    const got = againstSeaLevel({
      rows: [
        { altitude_m: 0, density: 1.225, range_m: 10000, hover_s: 1000 },
        { altitude_m: 1000, density: 1.11, range_m: 9500, hover_s: 900 },
        { altitude_m: 2000, density: 1.0, range_m: null, hover_s: null },
      ],
      usual_m: 300,
      flown_m: [250, 400],
    });
    expect(got.altitude).toEqual([0, 1000, 2000]);
    expect(got.range.map((v) => (v === null ? null : Math.round(v * 1000) / 1000))).toEqual([0, -0.05, null]);
    expect(got.hover.map((v) => (v === null ? null : Math.round(v * 1000) / 1000))).toEqual([0, -0.1, null]);
  });

  it("lays the minutes out by weight and every 1 m/s, hover first, gaps empty", () => {
    const got = minutesGrid({
      grid: [
        { mass_kg: 40.5, airspeed_ms: 0, minutes: 0.5 },
        { mass_kg: 32.5, airspeed_ms: 0, minutes: 1 },
        { mass_kg: 32.5, airspeed_ms: 3, minutes: 2 },
        { mass_kg: null, airspeed_ms: 2, minutes: 0.2 },
      ],
      sloped_minutes: 0,
      lofo: [],
      lofo_median: null,
      lofo_p90: null,
      targets: { median: 0.05, p90: 0.1 },
      level: "B",
      rows: 4,
      flights: 3,
      left_out: 0,
      skipped: {},
    });
    expect(got.speeds).toEqual([0, 1, 2, 3]);
    expect(got.rows.map((r) => r.mass_kg)).toEqual([32.5, 40.5, null]);
    expect(got.rows[0]!.cells).toEqual([1, null, null, 2]);
    expect(got.rows[2]!.cells).toEqual([null, null, 0.2, null]);
  });
});

describe("over time", () => {
  it("takes each figure's points in order, marked by the phase they come from", () => {
    const f = (n: number, hover: number | null, level: number | null, flagged: ("hover" | "level")[] = []) => ({
      flight_id: `f${n}`,
      n,
      hover,
      level,
      grams_per_watt: hover === null ? null : 8 / hover,
      figure_of_merit: hover === null ? null : 0.6 / hover,
      wh_per_km: level === null ? null : 100 * level,
      flagged,
    });
    const ot = {
      mass_kg: 32.5,
      density: 1.225,
      airspeed_ms: 9,
      flights: [f(1, 1, 1), f(3, 1.2, null, ["hover"]), f(4, null, 1.25, ["level"])],
      thresholds: { hover: 0.08, level: 0.08 },
      changes: [],
      count: 4,
    };
    expect(overTimeSeries(ot, "grams_per_watt")).toEqual([
      { n: 1, v: 8, flagged: false },
      { n: 3, v: 8 / 1.2, flagged: true },
    ]);
    expect(overTimeSeries(ot, "wh_per_km")).toEqual([
      { n: 1, v: 100, flagged: false },
      { n: 4, v: 125, flagged: true },
    ]);
  });
});
