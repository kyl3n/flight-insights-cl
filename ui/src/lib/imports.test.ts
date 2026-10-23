import { describe, expect, it } from "vitest";
import {
  decisions,
  fileStatus,
  initialChoice,
  initialChoices,
  matchKey,
  packList,
  resultCounts,
  undecided,
  whenText,
  type LogChoices,
  type Proposal,
  type ScannedFile,
} from "./imports";

const proposal = (extra: Partial<Proposal> = {}): Proposal => ({
  log: "f1",
  status: "ready",
  airframe_id: "af-1",
  airframe_name: "HL-09",
  confidence: "high",
  how: "board",
  score: 1,
  candidates: [],
  changes: [],
  note: null,
  ...extra,
});

const file = (extra: Partial<ScannedFile> = {}, p: Partial<Proposal> = {}): ScannedFile => ({
  id: "f1",
  name: "00000089.BIN",
  kind: "dataflash",
  size: 1,
  error: null,
  vehicle: "copter",
  firmware: "4.7.2",
  board: "CubeOrange",
  flights: [{ number: 1, key: "fl1-a", duration_s: 1260, boot_utc_us: 1_792_000_000_000_000, arm_us: 60_000_000, known: false }],
  boots: 0,
  param_writes: 0,
  proposal: proposal(p),
  ...extra,
});

const names = (p: string) => p.toUpperCase();

describe("first choices follow the proposal", () => {
  it("takes a matched airframe, holds what matches nothing and asks about a new board", () => {
    expect(initialChoice(file())).toEqual({ kind: "airframe", airframe_id: "af-1" });
    expect(initialChoice(file({}, { status: "no match", airframe_id: null }))).toEqual({ kind: "hold" });
    expect(initialChoice(file({}, { status: "decision" }))).toEqual({ kind: "undecided" });
    expect(initialChoice(file({}, { status: "duplicate" }))).toEqual({ kind: "skip" });
    expect(initialChoice(file({ kind: "tlog" }, { status: "unpaired" }))).toEqual({ kind: "import" });
    expect(initialChoice(file({ error: "bad" }, { status: "error" }))).toEqual({ kind: "skip" });
  });

  it("ticks every detected change to start with", () => {
    const f = file({}, { changes: [{ part: "INS_ACC_ID", before: "a", after: "b", evidence: "parameter INS_ACC_ID" }] });
    expect(initialChoices(f).confirmed).toEqual({ INS_ACC_ID: true });
  });
});

describe("the batch waits for decisions", () => {
  it("until every new board is decided and every new airframe named", () => {
    const f = file({}, { status: "decision" });
    const c: Record<string, LogChoices> = { f1: initialChoices(f) };
    expect(undecided([f], c)).toEqual([f]);
    c.f1!.choice = { kind: "new", platform_id: "p-1", name: " " };
    expect(undecided([f], c)).toEqual([f]);
    c.f1!.choice = { kind: "new", platform_id: "p-1", name: "HL-10" };
    expect(undecided([f], c)).toEqual([]);
  });
});

describe("decisions for import.commit", () => {
  it("carries the airframe, confirmed and ticked changes, packs, payload and station", () => {
    const f = file({}, { changes: [
      { part: "board", before: "known board", after: "new board", evidence: "system ID" },
      { part: "INS_ACC_ID", before: "a", after: "b", evidence: "parameter INS_ACC_ID" },
    ] });
    const c = initialChoices(f);
    c.confirmed.INS_ACC_ID = false;
    c.other = ["propellers"];
    c.otherReason = "failure";
    c.packs = "B-12, B-14, B-12";
    c.payload = " camera ";
    const [d] = decisions([f], { f1: c }, names, "gsv-1");
    expect(d).toEqual({
      log: "f1",
      action: "import",
      airframe_id: "af-1",
      station_version_id: "gsv-1",
      changes: [
        { part: "board", reason_kind: "detected", reason: "known board → new board" },
        { part: "PROPELLERS", reason_kind: "failure", reason: "" },
      ],
      packs: ["B-12", "B-14"],
      payload: "camera",
    });
  });

  it("skips undecided and skipped files, holds without an airframe, creates new airframes", () => {
    const files = [file(), file({ id: "f2" }), file({ id: "f3" }), file({ id: "f4", kind: "tlog" })];
    const c = Object.fromEntries(files.map((x) => [x.id, initialChoices(x)])) as Record<string, LogChoices>;
    c.f1!.choice = { kind: "undecided" };
    c.f2!.choice = { kind: "hold" };
    c.f3!.choice = { kind: "new", platform_id: "p-1", name: " HL-10 " };
    const out = decisions(files, c, names, null);
    expect(out[0]).toEqual({ log: "f1", action: "skip" });
    expect(out[1]).toEqual({ log: "f2", action: "import" });
    expect(out[2]).toMatchObject({ new_airframe: { platform_id: "p-1", name: "HL-10" } });
    expect(out[3]).toEqual({ log: "f4", action: "import" });
  });

  it("reads pack codes from a list typed by hand", () => {
    expect(packList(" B-1; B-2 ,, B-1 ")).toEqual(["B-1", "B-2"]);
  });
});

describe("the table's words", () => {
  it("shows status with a shape and a word", () => {
    expect(fileStatus(file(), { kind: "undecided" })).toEqual({ glyph: "▲", key: "import.status.decide" });
    expect(fileStatus(file(), { kind: "airframe", airframe_id: "a" }).glyph).toBe("●");
    expect(fileStatus(file({}, { status: "duplicate" }), { kind: "skip" }).key).toBe("import.status.duplicate");
    expect(fileStatus(file({}, { status: "error" }), { kind: "skip" }).glyph).toBe("■");
  });

  it("says how each log was matched", () => {
    expect(matchKey(proposal())).toBe("import.match.board");
    expect(matchKey(proposal({ status: "decision", how: "parameters" }))).toBe("import.match.newBoard");
    expect(matchKey(proposal({ status: "unpaired", how: "none" }))).toBe("import.match.unpaired");
  });

  it("dates a log by its take-off and gives its length", () => {
    const text = whenText(file(), "en", (n) => `${n} min`);
    expect(text).toMatch(/· 21 min$/);
    expect(whenText(file({ flights: [] }), "en", String)).toBe("");
  });

  it("counts the results", () => {
    const r = resultCounts([
      { log: "f1", status: "imported", flights: [], message: null },
      { log: "f2", status: "held", flights: [], message: null },
      { log: "f3", status: "imported", flights: [], message: null },
    ]);
    expect(r.imported).toBe(2);
    expect(r.held).toBe(1);
  });
});
