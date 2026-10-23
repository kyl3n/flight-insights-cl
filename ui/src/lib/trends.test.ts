import { describe, expect, it } from "vitest";
import { cardKey, changeTone, chartData, multiples, payloads, placeEvents, sparkPath, yRange, type Series, type TrendPoint } from "./trends";

const pt = (flight: string, value: number): TrendPoint => ({
  flight, utc_us: null, value, spread: {}, zone: "target", payload: null, good: true,
});

const S: Series = {
  airframe: "a", metric: "vibration.p95", segment: "hover", payload: null, unit: "m/s/s",
  direction: "lower", zones: { target: [[null, 30]], caution: [[30, 60]], out: [[60, null]] },
  source: { kind: "ardupilot", title: "Vibration" },
  points: [pt("f1", 12), pt("f2", 13), pt("f3", 14)],
  baseline: { low: 11, high: 15, median: 13, flights: 5 }, drift: null,
};

describe("trends", () => {
  it("places each event at the flight it first applies to, inside this series only", () => {
    const placed = placeEvents(S.points, [
      { kind: "firmware", flight: "f3", from: "4.7.0", to: "4.7.2" },
      { kind: "build", id: "b", flight: "elsewhere", number: 2, reason: null },
      { kind: "parameter change", id: "c", flight: null, origin: "loaded file", params: [] },
    ]);
    expect(placed.map((p) => [p.x, p.event.kind])).toEqual([[2, "firmware"]]);
  });

  it("frames the data with the zone edges near it, not the far ones", () => {
    const [lo, hi] = yRange(S);
    expect(lo).toBeLessThan(11);
    expect(hi).toBeLessThan(30); // 30 is 15 away from data spanning 4: left out
    const near = { ...S, points: [pt("f1", 26), pt("f2", 28)], baseline: null };
    expect(yRange(near)[1]).toBeGreaterThan(30);
  });

  it("lays out the chart rows, and small multiples on one shared scale", () => {
    expect(chartData(S)).toEqual([[0, 1, 2], [12, 13, 14]]);
    const other = { ...S, airframe: "b", points: [pt("g1", 26), pt("g2", 28)], apart: true };
    const m = multiples([S, other], 100, 50);
    expect(m.rows.map((r) => r.airframe)).toEqual(["a", "b"]);
    const lastY = (path: string) => Number(path.split(" ").at(-1));
    expect(lastY(m.rows[1]!.path)).toBeLessThan(lastY(m.rows[0]!.path)); // 28 sits above 14
    expect(m.bands.map((b) => b.zone)).toEqual(["target", "caution"]); // out (60+) is off the scale
    expect(multiples([], 100, 50)).toEqual({ bands: [], rows: [] });
  });

  it("draws sparklines, lists payloads and judges a change by the metric's direction", () => {
    expect(sparkPath([1])).toBe("");
    expect(sparkPath([0, 1], 10, 10)).toBe("M2.0 8.0 L8.0 2.0");
    expect(payloads([{ payload: "5 kg" }, { payload: null }, { payload: "2 kg" }, { payload: "5 kg" }])).toEqual([
      null, "2 kg", "5 kg",
    ]);
    expect(changeTone(2, "lower")).toBe("worse");
    expect(changeTone(-2, "lower")).toBe("better");
    expect(changeTone(2, "higher")).toBe("better");
    expect(changeTone(2, "band")).toBe("same");
    expect(cardKey({ metric: "m", segment: "hover" })).toBe("m@hover");
  });
});
