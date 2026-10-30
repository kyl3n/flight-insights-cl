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
  ambient_c: 22,
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
      tables: 1,
      ambient_c: null,
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
      tables: 1,
      ambient_c: null,
    });
  });

  it("reads a maker's whole table by its heading", () => {
    // the layout of T-Motor's pages, with made-up numbers: item and prop on the first row
    // only, the temperature after it, and power as its own column
    const sheet = [
      "Item No.\tProp\tThrottle\tVoltage(V)\tThrust(g)\tTorque(N*m)\tCurrent(A)\tRPM\tInputpower(W)\tEfficiency(g/W)\tOperatingTemperature℃)",
      "M1 KV100\tP22*7\t40%\t48.00\t2 000\t0.70\t5.00\t3400\t240.00\t8.33\t90℃(Ambient Temperature:20℃)",
      "50%\t47.90\t3000\t0.90\t8.00\t3900\t383.20\t7.83",
      "100%\t47.00\t9000\t3.00\t60.00\t7000\t2820.00\t3.19",
      "Notes: temperature after 10 mins at 80%, for reference only",
    ].join("\n");
    expect(parseSteps(sheet)).toEqual({
      steps: [
        { throttle_pct: 40, thrust_g: 2000, power_w: 240, rpm: 3400, torque_nm: 0.7, current_a: 5, volts: 48 },
        { throttle_pct: 50, thrust_g: 3000, power_w: 383.2, rpm: 3900, torque_nm: 0.9, current_a: 8, volts: 47.9 },
        { throttle_pct: 100, thrust_g: 9000, power_w: 2820, rpm: 7000, torque_nm: 3, current_a: 60, volts: 47 },
      ],
      refused: 0,
      tables: 1,
      ambient_c: 20,
    });
    // two setups under one heading
    const two = sheet.replace("Notes: temperature after 10 mins at 80%, for reference only", "M1 KV100\tP21*6\t40%\t48\t1800\t0.6\t4.5\t3500\t216\t8.3");
    expect(parseSteps(two).tables).toBe(2);
    // no power column: volts times amps; thrust in kg
    const noPower = "Throttle;Voltage (V);Current (A);Thrust (kg)\n50%;48;10;3,2\n60%;48;x;4";
    expect(parseSteps(noPower)).toEqual({ steps: [{ throttle_pct: 50, thrust_g: 3200, power_w: 480, current_a: 10, volts: 48 }], refused: 1, tables: 1, ambient_c: null });
    // torque in kgf·cm comes to N·m
    const kgfcm = "Throttle\tThrust(g)\tPower(W)\tTorque(kgf.cm)\tRPM\n50%\t3000\t380\t10\t3900";
    expect(parseSteps(kgfcm).steps).toEqual([{ throttle_pct: 50, thrust_g: 3000, power_w: 380, torque_nm: 0.980665, rpm: 3900 }]);
  });

  it("round-trips the maker's other columns through the form", () => {
    const rows = [
      { throttle_pct: 40, thrust_g: 2000, power_w: 240, rpm: 3400, torque_nm: 0.7, current_a: 5, volts: 48 },
      { throttle_pct: 60, thrust_g: 4000, power_w: 760, rpm: 4700, current_a: 16, volts: 47.5 },
      { throttle_pct: 100, thrust_g: 9000, power_w: 2820, rpm: 7000, torque_nm: 3, current_a: 60, volts: 47 },
    ];
    expect(valuesOf(draftOf({ ...TABLE, rows })).rows).toEqual(rows);
  });

  it("round-trips a table through the form", () => {
    const v = valuesOf(draftOf(TABLE));
    expect(v.rows).toEqual(TABLE.rows);
    expect(v).toMatchObject({ role: "current", kv: 210, prop_pitch_in: 7.4, volts: null, motor: "", ambient_c: 22 });
    expect(valuesOf(emptyDraft("candidate"))).toMatchObject({ role: "candidate", rows: [], kv: null });
  });

  it("gives hover thrust as a share of the most the table gives", () => {
    expect(hoverShare(4000, TABLE)).toBe(0.5);
  });
});
