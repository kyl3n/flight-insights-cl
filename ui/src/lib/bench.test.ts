import { describe, expect, it } from "vitest";
import { draftOf, emptyDraft, hoverShare, parseSteps, readNumber, valuesOf, type BenchTable } from "./bench";

const TABLE: BenchTable = {
  id: "b1",
  version: 2,
  role: "current",
  label: "made-up",
  motor: null,
  kv: 210,
  prop_diameter_in: 22,
  prop_pitch_in: 7.4,
  volts: null,
  motor_mass_g: null,
  source: null,
  rows: [
    { throttle_pct: 40, thrust_g: 2000, power_w: 120 },
    { throttle_pct: 70, thrust_g: 5000, power_w: 480 },
    { throttle_pct: 100, thrust_g: 8000, power_w: 990.5 },
  ],
  max_thrust_g: 8000,
  full_throttle: true,
};

describe("bench tables", () => {
  it("reads numbers as people type them", () => {
    expect(readNumber(" 22,5 ")).toBe(22.5);
    expect(readNumber("")).toBeNull();
    expect(readNumber("abc")).toBeNaN();
  });

  it("reads the steps pasted from a maker's sheet", () => {
    const tabs = "Throttle\tThrust (g)\tPower (W)\n50%\t3 000\t250\n60%\t4000g\t380W\n";
    expect(parseSteps(tabs)).toEqual({
      steps: [
        { throttle_pct: 50, thrust_g: 3000, power_w: 250 },
        { throttle_pct: 60, thrust_g: 4000, power_w: 380 },
      ],
      refused: 0,
    });
    expect(parseSteps("50;3000;250,5\r\n70;5000;480").steps).toEqual([
      { throttle_pct: 50, thrust_g: 3000, power_w: 250.5 },
      { throttle_pct: 70, thrust_g: 5000, power_w: 480 },
    ]);
    expect(parseSteps("50,3000,250\n\n70 5000 480").steps).toEqual([
      { throttle_pct: 50, thrust_g: 3000, power_w: 250 },
      { throttle_pct: 70, thrust_g: 5000, power_w: 480 },
    ]);
  });

  it("refuses a line with other columns rather than guess which is which", () => {
    // throttle, volts, amps, power, thrust: five numbers, in a maker's own order
    expect(parseSteps("50\t48\t5.2\t250\t3000\n70\t5000\t480")).toEqual({
      steps: [{ throttle_pct: 70, thrust_g: 5000, power_w: 480 }],
      refused: 1,
    });
  });

  it("round-trips a table through the form", () => {
    const v = valuesOf(draftOf(TABLE));
    expect(v.rows).toEqual(TABLE.rows);
    expect(v).toMatchObject({ role: "current", kv: 210, prop_pitch_in: 7.4, volts: null, motor: "" });
    expect(valuesOf(emptyDraft("candidate"))).toMatchObject({ role: "candidate", rows: [], kv: null });
  });

  it("gives hover thrust as a share of the most the table gives", () => {
    expect(hoverShare(4000, TABLE)).toBe(0.5);
  });
});
