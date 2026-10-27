import { describe, expect, it } from "vitest";
import {
  at,
  axis,
  commandTone,
  failsafeChecks,
  formatSeconds,
  incidentFinding,
  incidentText,
  lanes,
  modeSpans,
  nearMisses,
  placeTags,
  role,
  sentence,
  settingPairs,
  timingLook,
  unanswered,
  type FirstFault,
  type Mark,
  type Timeline,
  type TimelineEvent,
} from "./incidents";
import type { Analysis, EvidenceRow, FindingRow, Registry } from "./report";

// The SITL ground-station failsafe flight, as flight.timeline returns it (trimmed).
const unheard: TimelineEvent = {
  at_s: 9.89, kind: "gcs_unheard", to_s: 36.97, seconds: 27.1,
  words: { en: "the ground station unheard", uk: "наземну станцію не чути" },
};
const fired: TimelineEvent = {
  at_s: 14.94, kind: "error:FAILSAFE_GCS", code: 1,
  words: { en: "the GCS failsafe", uk: "аварійний режим наземної станції" },
};
const rtl: TimelineEvent = {
  at_s: 14.94, kind: "mode:GCS_FAILSAFE", mode: "RTL",
  words: { en: "RTL by gcs failsafe", uk: "RTL, причина: gcs failsafe" },
};
const ff: FirstFault = { chain: [unheard, fired], main: fired, explained: [rtl] };
const gcs: Timeline = {
  flight_id: "f",
  armed_s: 33.59,
  lanes: {
    link: [unheard],
    receiver: [],
    errors: [fired],
    battery: [],
    gnss_lost: [],
    params: [],
    mode: [
      { at_s: 0, mode: "GUIDED", reason: "GCS_COMMAND" },
      { at_s: 0, mode: "GUIDED", reason: "GCS_COMMAND" },
      { at_s: 14.94, mode: "RTL", reason: "GCS_FAILSAFE" },
    ],
    pilot_overrides: [],
    commands: [
      { at_s: 0, command: "COMPONENT_ARM_DISARM", result: "accepted", sender: 255 },
      { at_s: 20, command: "DO_SET_MODE", result: "denied", sender: 255 },
    ],
    warnings: [
      { at_s: -12.33, severity: 2, text: "PreArm: Need Position Estimate" },
      { at_s: 14.94, severity: 4, text: "GCS Failsafe" },
    ],
    gnss_fix: [{ from_s: 0.05, to_s: 37.25, value: 6 }],
  },
  first_fault: ff,
};
const seconds = (s: number) => formatSeconds(s, "en");

const finding = (over: Partial<FindingRow>): FindingRow => ({
  id: over.code ?? "x", analyzer: "a", code: "x", severity: "info", confidence: "high", params: {},
  evidence: [], changes: [], stage: null, ...over,
});
const evidence = (id: string, values: Record<string, unknown>): EvidenceRow => ({
  id, kind: "fact", summary: "", messages: [], fields: [], segment: null, values,
});
const facts = (over: Record<string, unknown>) => ({
  failsafe: "FAILSAFE_BATT", at_s: 14.3, lasted_s: null, mode_before: "GUIDED", response: "RTL", wanted: "RTL",
  allowed: ["RTL", "LAND"], setting: "BATT_FS_LOW_ACT, BATT_FS_CRT_ACT", setting_value: "2, 1", fs_options: 16,
  left_open: [], responded_after_s: 0, loss_from: "Volt under BATT_LOW_VOLT 11 V", loss_began_s: 12.2,
  waited_s: 2.1, wait_s: 2, late_s: 0, ...over,
});
const analysis = (findings: FindingRow[], ev: EvidenceRow[]): Analysis => ({
  run: "r", flight: "f", status: "complete", current: true, analyzers: {}, metrics: [], findings, evidence: ev,
});

describe("the incident timeline's lanes", () => {
  it("draws the lanes the log fills, in the screen's order", () => {
    const rows = lanes(gcs, "en", { firstFault: "first fault", seconds });
    expect(rows.map((l) => l.id)).toEqual([
      "link", "receiver", "pilot", "errors", "mode", "commands", "warnings", "gnss", "battery", "params",
    ]);
    const bare = { ...gcs, lanes: { ...gcs.lanes, pilot_overrides: undefined, mode: undefined, gnss_fix: undefined } };
    expect(lanes(bare, "en", { firstFault: "", seconds }).map((l) => l.id)).toEqual([
      "link", "receiver", "errors", "commands", "warnings", "battery", "params",
    ]);
  });

  it("labels the first fault where the chain starts, in the screen's language", () => {
    const [link] = lanes(gcs, "uk", { firstFault: "перша несправність", seconds });
    expect(link?.marks[0]).toMatchObject({
      from: 9.89, to: 36.97, tone: "out", label: "наземну станцію не чути · перша несправність",
    });
  });

  it("draws the most urgent of marks at the same moment on top", () => {
    const radio = { ...gcs, lanes: { ...gcs.lanes, errors: [fired, { ...fired, kind: "error:FLIGHT_MODE", words: { en: "a mode change refused", uk: "" } }] } };
    const errors = lanes(radio, "en", { firstFault: "", seconds }).find((l) => l.id === "errors");
    expect(errors?.marks.map((m) => [m.title, m.tone])).toEqual([
      ["a mode change refused", "caution"],
      ["the GCS failsafe", "out"],
    ]);
  });

  it("names only the commands that were not plainly accepted", () => {
    const commands = lanes(gcs, "en", { firstFault: "", seconds }).find((l) => l.id === "commands");
    expect(commands?.marks.map((m) => [m.tone, m.label])).toEqual([
      ["neutral", null],
      ["caution", "DO_SET_MODE denied"],
    ]);
    expect(commandTone("not supported")).toBe("info");
    expect(commandTone("in progress")).toBe("neutral");
  });

  it("draws the mode as spans, a repeated request being no change", () => {
    const spans = modeSpans(gcs.lanes.mode ?? [], gcs.armed_s, ff);
    expect(spans.map((m) => [m.label, m.from, m.to, m.tone])).toEqual([
      ["GUIDED", 0, 14.94, "neutral"],
      ["RTL", 14.94, 33.59, "out"], // the failsafe's, which the first fault explains
    ]);
    const own = modeSpans([{ at_s: 5, mode: "LAND", reason: "BATTERY_FAILSAFE" }], 10, null);
    expect(own[0]?.tone).toBe("caution"); // the aircraft's own change
  });

  it("puts every source on one axis, from before arming when something was logged then", () => {
    const a = axis(gcs);
    expect(a.start).toBeCloseTo(-12.33 - 0.74, 2); // with room either side
    expect(a.end).toBeCloseTo(37.25 + 0.74, 2);
    expect(a.ticks).toEqual([-10, 0, 10, 20, 30]);
    expect(at(a, a.start)).toBe(0);
    expect(at(a, 100)).toBe(100);
  });

  it("writes times in seconds after arming", () => {
    expect(formatSeconds(14.94, "en")).toBe("14.9 s");
    expect(formatSeconds(-12.33, "uk")).toBe("-12,3 с");
    expect(formatSeconds(0.022, "en")).toBe("0.02 s"); // a short wait still reads
    expect(sentence("the ground station unheard")).toBe("The ground station unheard");
  });
});

describe("the labels on a lane", () => {
  const mark = (key: string, from: number, label: string, to: number | null = null): Mark => ({
    key, from, to, tone: "caution", glyph: "◆", label, title: label,
    detail: { type: "event", lane: "errors", event: { ...fired, at_s: from } },
  });
  const a = { start: 0, end: 100, ticks: [] };

  it("joins the labels of marks at the same moment, and leaves a covered one out", () => {
    const tags = placeTags([mark("b", 10, "a mode change refused"), mark("a", 10, "the radio failsafe"), mark("c", 12, "too close")], a, 1000);
    expect([...tags.keys()]).toEqual(["b"]);
    expect(tags.get("b")?.text).toBe("a mode change refused · the radio failsafe");
    expect(placeTags([mark("a", 10, "one"), mark("b", 60, "two")], a, 1000).size).toBe(2);
  });

  it("draws a label that would run past the end to the left of its mark", () => {
    expect(placeTags([mark("a", 95, "the battery failsafe")], a, 1000).get("a")).toEqual({
      text: "the battery failsafe", flip: true, anchor: 95,
    });
    expect(placeTags([mark("s", 80, "a long label for a span", 99)], a, 600).get("s")?.anchor).toBe(99);
  });
});

describe("the first fault", () => {
  it("knows each event's place in the chain", () => {
    expect(role(ff, unheard)).toEqual({ kind: "first", step: 1, of: 2 });
    expect(role(ff, fired)).toEqual({ kind: "main", step: 2, of: 2 });
    expect(role(ff, rtl)?.kind).toBe("explained");
    expect(role(ff, { ...unheard, at_s: 3 })).toBeNull();
    const alone: FirstFault = { chain: [fired], main: fired, explained: [] };
    expect(role(alone, fired)?.kind).toBe("main");
  });

  it("words the finding with the events in the screen's language", () => {
    const f = finding({
      code: "incident.first_fault", severity: "caution",
      params: { first: "the ground station unheard", first_at_s: 9.9, main: "the GCS failsafe", main_at_s: 14.9 },
    });
    const reg = {
      findings: { "incident.first_fault": { title: { en: "First: {first} at {first_at_s} s", uk: "Перша: {first} на {first_at_s} с" }, action: { en: "{chain}", uk: "{chain}" }, stage: "operations" } },
    } as unknown as Registry;
    expect(incidentFinding(analysis([f], []))).toBe(f);
    expect(incidentText(reg, f, ff, "uk")).toEqual({
      title: "Перша: наземну станцію не чути на 9,9 с",
      action: "наземну станцію не чути → аварійний режим наземної станції",
    });
    expect(incidentText(reg, f, null, "en").title).toBe("First: the ground station unheard at 9.9 s");
  });
});

describe("did each failsafe work", () => {
  // The SITL battery flight: the low stage's RTL, then the critical stage's LAND, neither
  // logged resolved; each with its lead time.
  const low = evidence("failsafe:1", facts({ left_open: ["battery_stage"] }));
  const critical = evidence("failsafe:2", facts({ at_s: 19.6, mode_before: "RTL", response: "LAND", allowed: ["LAND"] }));
  const lead1 = evidence("precursor:lead:batt:14.3", { at_s: 14.3, watched: 2, warned: 2, headroom_lead_s: 2.1, low_voltage_lead_s: 1.3 });
  const lead2 = evidence("precursor:lead:batt:19.6", { at_s: 19.6, watched: 3, warned: 1, headroom_lead_s: null, charge_lead_s: null, low_voltage_lead_s: 6.6 });
  const a = analysis(
    [
      finding({ id: "w2", code: "failsafe.as_configured", evidence: ["failsafe:2"] }),
      finding({ id: "w1", code: "failsafe.as_configured", confidence: "medium", evidence: ["failsafe:1"] }),
      finding({ id: "late", code: "failsafe.late", severity: "caution", evidence: ["failsafe:2"] }),
      finding({ id: "l1", code: "failsafe.lead_time", evidence: [lead1.id] }),
      finding({ id: "l2", code: "failsafe.lead_time", evidence: [lead2.id] }),
      finding({ id: "sys", code: "system.failsafe", severity: "out", evidence: ["sys:err:6:1"] }),
    ],
    [low, critical, lead1, lead2],
  );

  it("takes each failsafe the engine judged, in the order it fired", () => {
    const checks = failsafeChecks(a);
    expect(checks.map((c) => [c.facts.at_s, c.verdict, c.finding.id])).toEqual([
      [14.3, "as_configured", "w1"],
      [19.6, "as_configured", "w2"],
    ]);
  });

  it("matches each with its lead time, earliest warning first", () => {
    const [first, second] = failsafeChecks(a);
    expect(first?.lead?.finding?.id).toBe("l1");
    expect(first?.lead?.precursors).toEqual([
      { id: "headroom", lead_s: 2.1 },
      { id: "low_voltage", lead_s: 1.3 },
    ]);
    expect(second?.lead?.precursors.map((p) => p.id)).toEqual(["low_voltage", "charge", "headroom"]);
  });

  it("names the failsafes still in force when one fired", () => {
    const [first, second] = failsafeChecks(a);
    expect(first?.others).toEqual([]);
    expect(second?.others.map((o) => o.at_s)).toEqual([14.3]); // never logged resolved
    const resolved = analysis(a.findings, [evidence("failsafe:1", facts({ lasted_s: 3 })), critical]);
    expect(failsafeChecks(resolved)[1]?.others).toEqual([]);
  });

  it("judges the trigger's timing by the engine's late finding", () => {
    const [first, second] = failsafeChecks(a);
    expect(first && timingLook(first)).toEqual({ status: "ok", key: "onTime" });
    expect(second && timingLook(second)).toEqual({ status: "caution", key: "late" });
    const quiet = analysis(
      [finding({ code: "failsafe.fell_back", severity: "caution", evidence: ["failsafe:1"] })],
      [evidence("failsafe:1", facts({ failsafe: "FAILSAFE_RADIO", loss_from: null, waited_s: null }))],
    );
    const [radio] = failsafeChecks(quiet);
    expect(radio?.verdict).toBe("fell_back");
    expect(radio && timingLook(radio)).toEqual({ status: "info", key: "unmeasured" });
    expect(radio?.lead).toBeNull();
  });

  it("pairs each setting with its value", () => {
    expect(settingPairs("BATT_FS_LOW_ACT, BATT_FS_CRT_ACT", "2, 1")).toEqual([
      ["BATT_FS_LOW_ACT", "2"],
      ["BATT_FS_CRT_ACT", "1"],
    ]);
    expect(settingPairs("FS_GCS_ENABLE", "1")).toEqual([["FS_GCS_ENABLE", "1"]]);
  });

  it("lists the losses no failsafe answered, and the near misses, by time", () => {
    const b = analysis(
      [
        finding({ id: "r", code: "failsafe.off_during_loss", severity: "safety", params: { at_s: 18.3 }, evidence: ["failsafe:loss:2"] }),
        finding({ id: "g", code: "failsafe.off_during_loss", params: { at_s: 10 }, evidence: ["failsafe:loss:1"] }),
        finding({ id: "rc", code: "link.rc_receiver_failsafes", severity: "caution", evidence: ["link:rc_failsafes"] }),
        finding({ id: "bat", code: "failsafe.battery_near_miss", severity: "caution", params: { at_s: 40 } }),
        finding({ id: "sil", code: "link.gcs_silence", severity: "caution", params: { at_s: 61.2 } }),
      ],
      [evidence("failsafe:loss:1", { began_s: 10 }), evidence("link:rc_failsafes", { count: 2, first_s: 22.5 })],
    );
    expect(unanswered(b).map((l) => [l.finding.id, l.evidence?.values.began_s ?? null])).toEqual([
      ["g", 10],
      ["r", null],
    ]);
    expect(nearMisses(b).map((n) => [n.kind, n.at_s])).toEqual([
      ["radio", 22.5],
      ["battery", 40],
      ["gcs", 61.2],
    ]);
  });
});
