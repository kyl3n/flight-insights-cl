import { describe, expect, it } from "vitest";
import { at, band, earlyEnds, flownSpan, interval, least, measuredAt, plot, split, weightChart, weights, type Figures, type Inputs } from "./range";

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
