import { describe, expect, it } from "vitest";
import type { Registry } from "./report";
import {
  alignCurves,
  clock,
  counts,
  formatChange,
  gateItems,
  leftOut,
  rowLook,
  scaleTop,
  withAlpha,
  type Gate,
  type InputEvent,
  type Inputs,
  type Row,
} from "./tune";

const row = (over: Partial<Row>): Row => ({
  metric: "tune.attitude_error_roll", family: "attitude_error", axis: "roll",
  this: { value: 28, low: 26, high: 30 }, compare: { value: 40, low: 37, high: 43 },
  change: -0.3, apart: true, verdict: "better", ...over,
});

const name = (en: string) => ({ name: { en, uk: en } });
const reg = {
  version: "t",
  metrics: {
    "tune.attitude_error_roll": name("Roll attitude tracking error"),
    "tune.d_noise_roll": name("Roll D-term noise share"),
  },
  findings: {},
} as unknown as Registry;

describe("the tune view", () => {
  it("writes a change signed, with a true minus", () => {
    expect(formatChange(-0.3051, "en")).toBe("−31%");
    expect(formatChange(0.03, "en")).toBe("+3%");
    expect(formatChange(0.001, "en")).toBe("±0%");
  });

  it("reads a row as the gate does: apart and large enough, short of it, or in the spread", () => {
    expect(rowLook(row({}), 0.2, "en")).toEqual({ status: "ok", text: "−30%, intervals apart" });
    const small = rowLook(row({ change: -0.12, verdict: "unchanged" }), 0.2, "en");
    expect(small).toEqual({ status: "caution", text: "−12%, under 20%" });
    const within = rowLook(row({ change: -0.03, apart: false, verdict: "unchanged" }), 0.2, "en");
    expect(within).toEqual({ status: "caution", text: "−3%, within the spread" });
    const worse = rowLook(row({ change: 0.4, verdict: "worse" }), 0.2, "en");
    expect(worse?.status).toBe("out");
    // The rate error is the attitude's guard: no change is fine.
    const guard = rowLook(row({ family: "tracking_error", change: 0, apart: false, verdict: "unchanged" }), 0.2, "en");
    expect(guard?.status).toBe("ok");
    expect(rowLook(row({ compare: null, change: undefined, verdict: undefined }), 0.2, "en")).toBeNull();
  });

  it("rounds the scale up past the largest interval", () => {
    expect(scaleTop([{ value: 31.4, high: 33.3 }, { value: 21.7 }])).toBe(50);
    expect(scaleTop([{ value: 7, high: 9.1 }])).toBe(10);
    expect(scaleTop([null])).toBe(1);
  });

  it("puts two flights' curves on one x axis, each stopping at its own band", () => {
    expect(alignCurves([{ x: [1, 2, 3], y: [0, -1, -3] }, { x: [1, 2], y: [0, -2] }])).toEqual([
      [1, 2, 3], [0, -1, -3], [0, -2, null],
    ]);
  });

  it("names a theme colour with an opacity for the canvas", () => {
    expect(withAlpha("#5ab0e8", 0.2)).toBe("rgba(90, 176, 232, 0.2)");
    expect(withAlpha("#fff", 1)).toBe("rgba(255, 255, 255, 1)");
    expect(withAlpha("teal", 0.5)).toBe("rgba(128, 128, 128, 0.5)");
  });

  it("counts the inputs and sums up those left out, by reason, first time first", () => {
    const e = (start_s: number, counted: boolean, reason: InputEvent["reason"]): InputEvent =>
      ({ start_s, end_s: start_s + 2, counted, reason, stick: 0.4 });
    const inputs: Inputs = {
      armed_s: 400,
      axes: {
        roll: [e(10, true, null), e(143, false, "saturated"), e(150, true, null)],
        pitch: [e(200, false, "saturated"), e(90, false, "low")],
      },
    };
    expect(counts(inputs)).toEqual({ roll: { counted: 2, found: 3 }, pitch: { counted: 0, found: 2 } });
    expect(leftOut(inputs)).toEqual([
      { reason: "low", count: 1, axes: ["pitch"], first_s: 90 },
      { reason: "saturated", count: 2, axes: ["roll", "pitch"], first_s: 143 },
    ]);
    expect(clock(143)).toBe("2:23");
    expect(clock(5.6)).toBe("0:06");
  });

  it("lists the gate's checks: attitude per axis, the guards merged, with each band", () => {
    const band = (group: string, before: number, after: number) =>
      ({ group, before_flight: "a", before, after_flight: "b", after, change: (before - after) / before, verdict: "better" as const });
    const gate: Gate = {
      id: "r", status: "applied", applied_flight: "f4", graded_flight: "f5", pending: true,
      checks: [
        { check: "metric_improves", metric: "tune.attitude_error_roll", by: 0.2, required: true, passed: null,
          bands: [band("band:0", 40, 28), { group: "band:2", before_flight: "c", before: 46, verdict: "waiting" }] },
        { check: "metric_not_worse", metric: "tune.tracking_error_roll", by: 0.2, passed: null, bands: [] },
        { check: "metric_not_worse", metric: "tune.tracking_error_pitch", by: 0.2, passed: true, bands: [] },
        { check: "no_shift_worse", passed: false, worse: true,
          shifts: [{ metric: "tune.d_noise_roll", group: "band:0", flight: "f5", cause: "ATC_RAT_RLL_D" }] },
        { check: "metric_vs_lightest", metric: "tune.rise_time_roll", max_ratio: 1.5, passed: null, bands: ["band:0"] },
      ],
      changes: [{ param: "ATC_RAT_RLL_FLTT", value: 10, status: "applied" }],
    };
    const items = gateItems(gate, reg, (g) => (g === "band:0" ? "light" : "loaded"), "en");
    expect(items.map((i) => [i.status, i.title])).toEqual([
      ["caution", "Roll attitude tracking error at least 20% lower in every band, intervals apart"],
      ["caution", "Rate tracking error no worse (roll, pitch): not 20% worse with the intervals apart"],
      ["out", "Nothing else shifted worse beyond this airframe's spread"],
      ["caution", "Loaded rise time within 1.5× the light flight's (roll)"],
      ["caution", "The plan's changes that prepared it"],
    ]);
    expect(items[0]!.detail).toBe("light: 40% → 28% (−30%, better) · loaded: not flown again yet");
    expect(items[2]!.detail).toBe("Roll D-term noise share worse, most likely ATC_RAT_RLL_D");
    expect(items[3]!.detail).toBe("needs a light and a loaded tune check");
    expect(items[4]!.detail).toBe("ATC_RAT_RLL_FLTT 10: applied, waiting for its grade");
  });
});
