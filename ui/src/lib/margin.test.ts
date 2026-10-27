import { describe, expect, it } from "vitest";
import { bearing, fill, isSkipped, lossLook, partners, side, stations, type MotorSlot } from "./margin";

// ArduPilot's OCTAQUAD X (FRAME_CLASS 4, FRAME_TYPE 1), as knowledge/frames/4.7.0.json has it
const x8: MotorSlot[] = (
  [
    [1, -0.5, 0.5],
    [2, 0.5, 0.5],
    [3, 0.5, -0.5],
    [4, -0.5, -0.5],
    [5, 0.5, 0.5],
    [6, -0.5, 0.5],
    [7, -0.5, -0.5],
    [8, 0.5, -0.5],
  ] as const
).map(([motor, roll, pitch]) => ({ motor, roll, pitch, yaw: 0.5, needed_pct: 80 }));

// HEXA X (FRAME_CLASS 2, FRAME_TYPE 1)
const hexa = (
  [
    [1, -0.5, 0],
    [2, 0.5, 0],
    [3, 0.25, 0.5],
    [4, -0.25, -0.5],
    [5, -0.25, 0.5],
    [6, 0.25, -0.5],
  ] as const
).map(([motor, roll, pitch]) => ({ motor, roll, pitch, yaw: 0.5, needed_pct: null }));

describe("where each motor sits", () => {
  it("reads the mixer's factors as a bearing from the nose", () => {
    expect(bearing({ roll: -0.5, pitch: 0.5 })).toBeCloseTo(45);
    expect(bearing({ roll: 0.5, pitch: -0.5 })).toBeCloseTo(225);
    expect(bearing({ roll: -0.5, pitch: 0 })).toBeCloseTo(90);
  });

  it("names the side: front right is a negative roll factor and a positive pitch one", () => {
    expect(x8.map(side)).toEqual([
      "frontRight", "frontLeft", "rearLeft", "rearRight", "frontLeft", "frontRight", "rearRight", "rearLeft",
    ]);
    expect(hexa.map(side)).toEqual(["right", "left", "frontLeft", "rearRight", "frontRight", "rearLeft"]);
  });

  it("puts a coaxial pair on one arm", () => {
    const arms = stations(x8);
    expect(arms.map((s) => s.motors)).toEqual([[1, 6], [2, 5], [3, 8], [4, 7]]);
    expect(arms[0]?.x).toBeCloseTo(Math.SQRT1_2);
    expect(arms[0]?.y).toBeCloseTo(-Math.SQRT1_2);
    expect(partners(x8, 4)).toEqual([7]);
    expect(stations(hexa)).toHaveLength(6);
    expect(partners(hexa, 3)).toEqual([]);
  });
});

describe("each loss against the target", () => {
  it("meets it, goes over it, or has no balance at all", () => {
    expect(lossLook(80, 85)).toEqual({ status: "ok", label: "margin.loss.meets" });
    expect(lossLook(85, 85).status).toBe("ok");
    expect(lossLook(85.7, 85)).toEqual({ status: "caution", label: "margin.loss.over" });
    expect(lossLook(null, 85)).toEqual({ status: "out", label: "margin.loss.noBalance" });
  });

  it("fills a bar within its width", () => {
    expect(fill(88)).toBe(88);
    expect(fill(130)).toBe(100);
    expect(fill(null)).toBe(0);
  });

  it("tells a skipped part from one with numbers", () => {
    expect(isSkipped({ skipped: "a QUAD/X cannot fly on one motor fewer" })).toBe(true);
    expect(isSkipped({ hover_thrust_pct: 43 })).toBe(false);
    expect(isSkipped(null)).toBe(false);
  });
});
