import { describe, expect, it } from "vitest";
import { align, groupErrors, motorColour, standout, zoomTo, type MotorRow } from "./flightviews";

const ctx = {
  window_s: [0, 100] as [number, number], armed_s: [5, 95] as [number, number],
  hover_s: [[20, 30], [40, 60]] as [number, number][], modes: [], simulated: false, units: {},
};

describe("flight views", () => {
  it("puts traces with different bins on one time axis, with gaps", () => {
    const a = { label: "a", colour: "#000", env: { t: [1, 2, 3], lo: [0, 1, 2], hi: [1, 2, 3] } };
    const b = { label: "b", colour: "#000", env: { t: [2, 4], lo: [5, 6], hi: [7, 8] } };
    expect(align([a, b])).toEqual([[1, 2, 3, 4], [0, 1, 2, null], [1, 2, 3, null], [null, 5, null, 6], [null, 7, null, 8]]);
  });

  it("zooms to the hover windows, or shows the whole flight", () => {
    expect(zoomTo(ctx, "flight")).toEqual([0, 100]);
    expect(zoomTo(ctx, "hover")).toEqual([18, 62]); // 5% of the 40 s span either side
    expect(zoomTo({ ...ctx, hover_s: [] }, "hover")).toEqual([0, 100]);
  });

  it("picks out a motor only when it is clearly above its siblings", () => {
    const rows = (values: (number | null)[]): MotorRow[] => values.map((v, i) => ({ number: i + 1, channel: i + 1, hover_output: v }));
    expect(standout(rows([40, 41, 52, 40]), (m) => m.hover_output)).toBe(3);
    expect(standout(rows([40, 41, 41.5, 40]), (m) => m.hover_output)).toBeNull();
    expect(standout(rows([40, null]), (m) => m.hover_output)).toBeNull();
  });

  it("groups repeated errors and keeps their first time", () => {
    const e = (t: number, code: number) => ({ t, subsystem: "CRASH_CHECK", code, meaning: "m" });
    const grouped = groupErrors([e(12, 1), e(5, 2), e(30, 1)]);
    expect(grouped.map((g) => [g.t, g.code, g.count])).toEqual([[5, 2, 1], [12, 1, 2]]);
    expect(motorColour(1)).toBe(motorColour(13));
  });
});
