import { describe, expect, it } from "vitest";
import {
  boundary,
  buildRows,
  candidateName,
  candidateWhy,
  changeItems,
  effectText,
  factorsText,
  fleetLink,
  flightDate,
  flightNumbers,
  hardwareWith,
  intervalText,
  LIST_CAP,
  matches,
  miniChart,
  origins,
  paramValue,
  rowTitle,
  scoreShare,
  shiftKey,
  verdictLook,
  weatherRows,
  type Credit,
  type HistoryData,
  type RevertTest,
  type Shift,
  type ShiftCandidate,
  type Split,
} from "./history";
import type { FlightRow } from "./report";

const flight = (id: string, n: number): FlightRow => ({
  id, airframe_id: "a", airframe: "HL-07", utc_us: Date.UTC(2026, 8, n) * 1000, duration_s: 600, firmware: "4.7.0",
  payload: null, modes: [], analysis: null,
});
// flight.list order: newest first. f1 is the oldest.
const FLIGHTS = ["f7", "f6", "f5", "f4", "f3", "f2", "f1"].map((id) => flight(id, Number(id.slice(1))));

const cand = (over: Partial<ShiftCandidate>): ShiftCandidate => ({
  kind: "parameter", name: "INS_HNTCH_BW", before: 40, after: 20, flight: "f3", class: "tuning", origin: "loaded file",
  score: 0.9, link: "harmonic notches", why: { en: "Notch reason", uk: "Причина" }, factors: { strength: 0.6, direction: 0.6, size: 1, timing: 1, fleet: 1.5 },
  fleet: { agree: 3, against: 0 }, ...over,
});

const shift = (over: Partial<Shift> = {}): Shift => ({
  metric: "tune.d_noise_roll", name: { en: "Roll D-term noise share", uk: "Частка шуму" }, unit: "%", segment: "hover",
  group: "band:0", flight: "f3", before: ["f1", "f2"], after: ["f3", "f4"], effect: 5.9, low: 5.5, high: 6.5, spread: 0.3,
  verdict: "worse", confidence: "medium",
  candidates: [cand({}), cand({ kind: "build", name: "build 2", before: null, after: "props replaced", class: null, origin: null, score: 0.5, link: null, why: null, factors: { strength: 0.5, timing: 1 }, fleet: null })],
  context: [{ kind: "parameter", name: "MOT_THST_HOVER", before: 0.31, after: 0.33, flight: "f3", class: "learned", origin: "loaded file" }],
  weather: null, ...over,
});

const revert: RevertTest = {
  metric: "tune.d_noise_roll", name: { en: "Roll D-term noise share", uk: "Частка шуму" }, unit: "%", segment: "hover",
  group: "band:0", flight: "f3", before: 10.1, after: 16, band: 0.6, explained_by: "INS_HNTCH_BW", next_step: null,
  steps: [
    { kind: "parameter", name: "INS_HNTCH_BW", before: 40, after: 20, class: "tuning", order: 1, state: "explained", score: 0.9, tested_on: "f5", value: 10.3 },
    { kind: "parameter", name: "BATT_CAPACITY", before: 10000, after: 12000, class: "tuning", order: 2, state: "not needed", score: 0.05, tested_on: null, value: null },
  ],
  by_hand: [{ kind: "build", name: "build 2", before: null, after: "props replaced", class: null }],
};

const credit = (over: Partial<Credit> = {}): Credit => ({
  metric: "tune.tracking_error_roll", name: { en: "Roll rate tracking error", uk: "Похибка" }, unit: "%", segment: "inputs",
  group: "band:0", flight: "f6", effect: -6.1, confidence: "medium", param: "ATC_RAT_RLL_P", before: 0.135, after: 0.162,
  link: "rate P", kept: true, undone_on: null, value_then: null, ...over,
});

const split: Split = {
  at_s: 31.6,
  changes: [
    { name: "LOG_BITMASK", before: 180223, after: 180222, class: "tuning", origin: "in flight" },
    { name: "ATC_RAT_RLL_P", before: 0.13500000536441803, after: 0.05400000140070915, class: "tuning", origin: "tuning knob" },
  ],
  context: [], before_s: [0, 31.6], after_s: [34, 66.3], skipped: null,
  results: [{
    metric: "tune.tracking_error_roll", name: { en: "Roll rate tracking error", uk: "Похибка" }, unit: "%", segment: "inputs",
    method: "intervals", before: 25.1, after: 36.4, effect: 11.4, low: 11.3, high: 11.5, verdict: "worse", confidence: "high",
    candidates: [{ name: "ATC_RAT_RLL_P", before: 0.135, after: 0.054, class: "tuning", origin: "tuning knob", score: 1, link: "rate P", why: null, factors: { strength: 1 } }],
  }],
};

const DATA: HistoryData = {
  flights: FLIGHTS,
  events: [
    { kind: "parameter change", id: "c3", flight: "f3", origin: "loaded file", params: ["INS_HNTCH_BW", "BATT_CAPACITY", "MOT_THST_HOVER"],
      changes: [
        { name: "INS_HNTCH_BW", before: 40, after: 20, class: "tuning" },
        { name: "BATT_CAPACITY", before: 10000, after: 12000, class: "tuning" },
        { name: "MOT_THST_HOVER", before: 0.31, after: 0.33, class: "learned" },
      ] },
    { kind: "build", id: "b2", flight: "f3", number: 2, reason: "props replaced" },
    { kind: "parameter change", id: "c4", flight: "f4", origin: "unexplained", params: ["STAT_BOOTCNT"],
      changes: [{ name: "STAT_BOOTCNT", before: 41, after: 42, class: "counter" }] }, // only a counter: no entry
    { kind: "parameter change", id: "c5", flight: "f5", origin: "ground station edit", params: ["INS_HNTCH_BW"],
      changes: [{ name: "INS_HNTCH_BW", before: 20, after: 40, class: "tuning" }] },
    { kind: "parameter change", id: "c6", flight: "f6", origin: "unexplained", params: ["ATC_RAT_RLL_P"],
      changes: [{ name: "ATC_RAT_RLL_P", before: 0.135, after: 0.162, class: "tuning" }] },
    { kind: "firmware", flight: "f7", from: "4.7.0", to: "4.7.2" },
    { kind: "recommendation", id: "r1", flight: "f7", param: null, proposed: null, grade: "confirmed" },
    { kind: "firmware", flight: "elsewhere", from: "4.7.0", to: "4.7.2" }, // not this airframe's listed flights
  ],
  shifts: [
    shift(),
    // first measured on f4, though its changes first applied on f3
    shift({ metric: "esc.peak_temperature", name: { en: "ESC peak temperature", uk: "ESC" }, unit: "degC", segment: "flight", flight: "f4" }),
    shift({ metric: "tune.tracking_error_roll", segment: "inputs", flight: "f6", verdict: "better", effect: -6.1, spread: null,
      candidates: [cand({ name: "ATC_RAT_RLL_P", flight: "f6", before: 0.135, after: 0.162 })] }),
  ],
  credits: [credit({ kept: false, undone_on: "f7", value_then: 0.135 })],
  reverts: [revert],
  splits: { f7: [split] },
  recs: [{ id: "r1", finding_code: "tune.autotune_offer", param: null, detail: { applied_flight: "f5" } }],
};

describe("history", () => {
  it("numbers flights in flying order, unless the list was cut at its cap", () => {
    const n = flightNumbers(FLIGHTS);
    expect([n.get("f1"), n.get("f7")]).toEqual([1, 7]);
    const many = Array.from({ length: LIST_CAP }, (_, i) => flight(`g${i}`, 1));
    expect(flightNumbers(many).size).toBe(0);
  });

  it("places a shift at the boundary its own changes applied at", () => {
    expect(boundary(shift({ flight: "f4" }))).toBe("f3");
    const nearby = shift({ candidates: [cand({ flight: "f2", factors: { timing: 0.5 } })] });
    expect(boundary(nearby)).toBe("f3"); // no candidate at full timing: its first flight after
  });

  it("builds one entry per flight start and per in-flight split, newest first", () => {
    const rows = buildRows(DATA);
    expect(rows.map((r) => r.key)).toEqual(["f7|ab|31.6", "f7|start", "f6|start", "f5|start", "f3|start"]);
    const f3 = rows.find((r) => r.key === "f3|start")!;
    expect(f3.sets[0]!.changes.map((c) => c.name)).toEqual(["INS_HNTCH_BW", "BATT_CAPACITY"]);
    expect(f3.sets[0]!.aside.map((c) => c.name)).toEqual(["MOT_THST_HOVER"]);
    expect(f3.builds).toEqual([{ number: 2, reason: "props replaced" }]);
    // Both worse shifts sit at f3, the ESC one too though first measured on f4.
    expect(f3.shifts.map((s) => s.metric)).toEqual(["tune.d_noise_roll", "esc.peak_temperature"]);
    expect(rows.find((r) => r.key === "f5|start")!.readings.map((x) => x.step.name)).toEqual(["INS_HNTCH_BW"]);
  });

  it("tags the change after an AutoTune flight and the AutoTune's grade", () => {
    const rows = buildRows(DATA);
    expect(rows.find((r) => r.key === "f6|start")!.afterAutotune).toBe(true); // AutoTune flown on f5
    const f7 = rows.find((r) => r.key === "f7|start")!;
    expect(f7.grades).toEqual([{ id: "r1", param: null, proposed: null, grade: "confirmed", autotune: true, doneOn: "f5" }]);
  });

  it("puts a credit with its shift, and names the flight that undid it", () => {
    const rows = buildRows(DATA);
    expect(rows.find((r) => r.key === "f6|start")!.credits).toHaveLength(1);
    expect(rows.find((r) => r.key === "f7|start")!.undoes.map((c) => c.param)).toEqual(["ATC_RAT_RLL_P"]);
  });

  it("filters entries by what they hold", () => {
    const rows = buildRows(DATA);
    const keys = (f: Parameters<typeof matches>[1]) => rows.filter((r) => matches(r, f)).map((r) => r.key);
    expect(keys("all")).toHaveLength(5);
    expect(keys("params")).toEqual(["f7|ab|31.6", "f6|start", "f5|start", "f3|start"]);
    expect(keys("build")).toEqual(["f3|start"]);
    expect(keys("firmware")).toEqual(["f7|start"]);
    expect(keys("autotune")).toEqual(["f7|start", "f6|start"]);
    expect(keys("effect")).toEqual(["f7|ab|31.6", "f6|start", "f5|start", "f3|start"]);
    const inconclusive = buildRows({ ...DATA, shifts: [shift({ verdict: "inconclusive" })], reverts: [], splits: {} });
    expect(inconclusive.filter((r) => matches(r, "effect"))).toEqual([]);
  });

  it("titles each entry", () => {
    const rows = buildRows(DATA);
    const title = (key: string) => rowTitle(rows.find((r) => r.key === key)!, "en");
    expect(title("f7|ab|31.6")).toBe("In-flight A/B: ATC_RAT_RLL_P");
    expect(title("f5|start")).toBe("Revert test: INS_HNTCH_BW");
    expect(title("f3|start")).toBe("3 changes at once");
    expect(title("f6|start")).toBe("ATC_RAT_RLL_P");
    expect(title("f7|start")).toBe("Firmware 4.7.0 → 4.7.2");
    const graded = buildRows({ ...DATA, events: DATA.events.filter((e) => e.kind === "recommendation"), splits: {}, credits: [] });
    expect(rowTitle(graded[0]!, "en")).toBe("AutoTune graded");
    expect(rowTitle(graded[0]!, "uk")).toBe("AutoTune оцінено");
  });

  it("lists what changed, with the knob and calibration noted, and no list where the title says it", () => {
    const rows = buildRows(DATA);
    const ab = changeItems(rows[0]!, "en");
    expect(ab.map((x) => [x.text, x.note])).toEqual([
      ["LOG_BITMASK 180223 → 180222", null],
      ["ATC_RAT_RLL_P 0.135 → 0.054", "tuning knob"],
    ]);
    const f3 = changeItems(rows.find((r) => r.key === "f3|start")!, "en");
    expect(f3.map((x) => x.text)).toEqual(["INS_HNTCH_BW 40 → 20", "BATT_CAPACITY 10000 → 12000", "Build 2: props replaced"]);
    expect(changeItems(rows.find((r) => r.key === "f7|start")!, "en")).toEqual([]);
    const cal = buildRows({ ...DATA, events: [{ kind: "parameter change", id: "x", flight: "f2", origin: "loaded file", params: ["INS_ACCOFFS_X"],
      changes: [{ name: "INS_ACCOFFS_X", before: 0.1, after: 0.2, class: "calibration" }] }], shifts: [], credits: [], reverts: [], splits: {} });
    expect(changeItems(cal[0]!, "en")[0]!.note).toBe("calibration");
  });

  it("names where the changes came from", () => {
    const rows = buildRows(DATA);
    expect(origins(rows[0]!, "en")).toEqual(["ground station, in flight", "tuning knob"]);
    expect(origins(rows.find((r) => r.key === "f3|start")!, "en")).toEqual(["loaded file", "Build 2"]);
    expect(origins(rows.find((r) => r.key === "f7|start")!, "uk")).toEqual(["оновлення прошивки"]);
  });

  it("writes parameter values as they were set, without float noise", () => {
    expect(paramValue(0.13500000536441803, "en")).toBe("0.135");
    expect(paramValue(0.003599999938160181, "en")).toBe("0.0036");
    expect(paramValue(180223, "en")).toBe("180223");
    expect(paramValue(0.135, "uk")).toBe("0,135");
    expect(paramValue(null, "en")).toBe("–");
    expect(paramValue("props replaced", "en")).toBe("props replaced");
  });

  it("writes a difference with its sign, a percentage in points, and its interval", () => {
    expect(effectText(5.9, "%", "en")).toBe("+5.9 pp");
    expect(effectText(-1.6, "%", "uk")).toBe("−1,6 в. п.");
    expect(effectText(5.25, "degC", "en")).toBe("+5.25 °C");
    expect(effectText(-3, "count", "en")).toBe("−3");
    expect(intervalText(-2.15, 0.1, "en")).toBe("−2.15 to +0.1");
  });

  it("reads a verdict by how it was measured", () => {
    expect(verdictLook("worse", "spread", "en")).toEqual({ status: "out", text: "Worse, beyond this airframe's spread" });
    expect(verdictLook("better", "pair", "en")).toEqual({ status: "ok", text: "Better, intervals apart" });
    expect(verdictLook("moved", "blocks", "en")).toEqual({ status: "info", text: "Moved, beyond the blocks' spread" });
    expect(verdictLook("inconclusive", "spread", "en").text).toBe("Inconclusive: the interval includes zero");
    expect(verdictLook("inconclusive", "intervals", "en").text).toBe("Inconclusive: the intervals overlap");
  });

  it("shows the ranking's factors and score as the engine multiplied them", () => {
    expect(factorsText({ timing: 1, fleet: 1.5, strength: 0.6, direction: 0.6, size: 1 }, "en")).toBe(
      "map 0.6 × direction 0.6 × size 1 × timing 1 × fleet 1.5",
    );
    expect(scoreShare(0.45, [0.9, 0.45])).toBe(0.45);
    expect(scoreShare(0.75, [1.5, 0.75])).toBe(0.5); // a fleet-raised top sets the scale
  });

  it("names each candidate and says why the map ranked it", () => {
    const [notch, build] = shift().candidates as [ShiftCandidate, ShiftCandidate];
    expect(candidateName(build, "en")).toBe("Build 2");
    expect(candidateName(build, "uk")).toBe("Збірка 2");
    expect(candidateName({ kind: "firmware", name: "firmware", before: "4.7.0", after: "4.7.2" }, "en")).toBe("Firmware 4.7.0 → 4.7.2");
    expect(candidateWhy(notch, "D noise", "uk")).toBe("Причина");
    expect(candidateWhy(build, "D noise", "en")).toBe("props replaced");
    expect(candidateWhy({ ...notch, why: null, link: null }, "D noise", "en")).toBe("Not linked to D noise in the parameter map.");
    expect(candidateWhy({ ...notch, why: null, link: "fleet" }, "D noise", "en")).toContain("learned link");
  });

  it("finds the platform's confirmed link the way the change and the metric went", () => {
    const links = [{ param: "INS_HNTCH_BW", changed: "down" as const, metric: "tune.d_noise_roll", moved: "up" as const, airframes: 4, against: 0, in_map: true, link: "harmonic notches" }];
    expect(fleetLink(links, cand({}), "tune.d_noise_roll", 5.9)?.airframes).toBe(4);
    expect(fleetLink(links, cand({ before: 20, after: 40 }), "tune.d_noise_roll", 5.9)).toBeNull();
    expect(fleetLink(links, cand({}), "tune.d_noise_roll", -5.9)).toBeNull();
  });

  it("lists what confounds a shift", () => {
    expect(hardwareWith(shift()).map((c) => c.name)).toEqual(["build 2"]);
    const w = weatherRows({ values: { wind_ms: [3, 7.5], density_altitude_m: [420, 430] }, differs: ["wind_ms"] }, "en");
    expect(w).toEqual([
      { name: "Wind", before: "3 m/s", after: "7.5 m/s", differs: true },
      { name: "Density altitude", before: "420 m", after: "430 m", differs: false },
    ]);
    expect(weatherRows(null, "en")).toEqual([]);
  });

  it("keys a shift the way the revert test and the credit name it", () => {
    expect(shiftKey(shift())).toBe(shiftKey(revert));
    expect(shiftKey(credit())).toBe("tune.tracking_error_roll|inputs|band:0|f6");
  });

  it("draws the metric around a shift in a small box", () => {
    const c = miniChart([{ flight: "a", value: 10 }, { flight: "b", value: 20 }, { flight: "c", value: 10 }], { low: 9, high: 11 }, 100, 50);
    expect(c.dots.map((d) => d.x)).toEqual([8, 50, 92]);
    expect(c.dots[1]!.y).toBeLessThan(c.dots[0]!.y); // higher values sit higher
    expect(c.band!.h).toBeGreaterThan(0);
    expect(c.x("b")).toBe(50);
    expect(c.x("z")).toBeNull();
    expect(c.path.startsWith("M8.0 ")).toBe(true);
  });

  it("dates a flight without its time", () => {
    expect(flightDate(Date.UTC(2026, 9, 24, 9) * 1000, "en")).toBe("24 Oct 2026");
    expect(flightDate(null, "en")).toBeNull();
  });
});
