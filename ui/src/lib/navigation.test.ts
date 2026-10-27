import { describe, expect, it } from "vitest";
import {
  AREA_LOOK,
  actionSteps,
  areaText,
  copingText,
  eventFacts,
  firstPick,
  goalLook,
  picked,
  receiversLabel,
  rowLabel,
  span,
  verdictWord,
  type AreaRow,
  type CopingRow,
  type NavEvent,
  type NavigationView,
  type Outage,
} from "./navigation";

// Shaped as the engine's flight.navigation gives them for the SITL jam and drills.
const event = (over: Partial<NavEvent> = {}): NavEvent => ({
  n: 1, class: "jamming", confidence: "medium", start_s: 23.5, end_s: 59.2, duration_s: 35.8,
  receivers: [1], channels: ["fix", "sats"], peaks: { fewest_sats: 3, worst_hacc_m: 299.96 },
  position: { largest_track_gap_m: 1453, estimator_resets: 9, largest_reset_m: 1125 },
  altitude_gap_m: null, time: null, finding: null, heading_finding: null, outages: [1],
  coping: [], evidence: ["gnss:event:1"], ...over,
});
const outage = (over: Partial<Outage> = {}): Outage => ({
  n: 1, drill: true, start_s: 24, end_s: 44.4, duration_s: 20.4, using_gnss_at_start: true,
  detection_s: 3.9, absolute_s: 6.9, kept_s: 6.9, held: false, kept_relative: false,
  resumed_s: 0.1, source_set: null, failsafes: [], mode: "LAND", mode_reason: "EKF_FAILSAFE",
  drift: null, finding: null, events: [], coping: [], evidence: [], ...over,
});
const view = (over: Partial<NavigationView> = {}): NavigationView => ({
  flight: "f", airframe_id: "a", current: true,
  skipped: { gnss: null, no_gnss: null, no_gnss_readiness: null },
  events: [], outages: [], receivers: [],
  independent: { estimator: true, barometer: true, second_receiver: false },
  readiness: [], actions: [], sources: [], ...over,
});
const row = (over: Partial<CopingRow>): CopingRow => ({ id: "estimator", status: "info", verdict: "rejected", values: {}, ...over });
const area = (over: Partial<AreaRow>): AreaRow => ({
  area: "position", status: "none", say: "none", fill: {}, values: {}, evidence: "no_gnss:readiness:position", ...over,
});

describe("what How the aircraft coped shows", () => {
  it("opens on the first event, else the first outage or drill", () => {
    expect(firstPick(view())).toBeNull();
    expect(firstPick(view({ outages: [outage({ n: 2 })] }))).toEqual({ kind: "outage", n: 2 });
    const both = view({ events: [event()], outages: [outage()] });
    expect(firstPick(both)).toEqual({ kind: "event", n: 1 });
    expect(picked(both, { kind: "outage", n: 1 })).toMatchObject({ drill: true });
    expect(picked(both, { kind: "event", n: 7 })).toBeNull();
  });

  it("words each row from the engine's verdict, in both languages", () => {
    const rejected = row({ values: { rejected_s: 24.8, ratio: 117.99 } });
    expect(rowLabel(rejected, "en")).toBe("The estimator");
    expect(copingText(rejected, "en")).toBe("rejected GNSS for 24.8 s; test ratio up to 118");
    expect(copingText(rejected, "uk")).toBe("відкидав GNSS протягом 24,8 с; тестове відношення до 118");
    expect(verdictWord(rejected, "en")).toBe("Rejected");
    const followed = row({ status: "out", verdict: "followed", values: { innovation: 0.53 } });
    expect(copingText(followed, "en")).toContain("never rejected it; velocity innovation up to 0.53 m/s");
    const onFlow = row({ verdict: "not_using", values: { set: 2 } });
    expect(copingText(onFlow, "en")).toBe("was not using GNSS when it went (source set 2)");
    const drift = row({ id: "drift", status: "ok", verdict: "measured", values: { drift_m: 0.1, rate: 0.2, minutes: 50 } });
    expect(copingText(drift, "en")).toBe("0.1 m from GNSS when it returned; 0.2 m a minute, off by the landing tolerance after 50 min");
    const heading = row({ id: "heading", status: "ok", verdict: "ready", values: { compasses: 3, no_reset: true } });
    expect(copingText(heading, "en")).toBe(
      "no heading reset from GNSS velocity; the heading does not depend on GNSS (compasses in use: 3)",
    );
    const mode = row({ id: "mode", verdict: "changed", values: { mode: "LAND", reason: "EKF_FAILSAFE" } });
    expect(copingText(mode, "en")).toBe("LAND, by EKF_FAILSAFE");
  });

  it("lists judged failsafes by their own findings, worded by the status the engine gave", () => {
    const judged = row({ id: "failsafe", status: "ok", verdict: "judged", values: { judged: [], names: [] } });
    expect(copingText(judged, "en")).toBe("");
    expect(verdictWord(judged, "en")).toBe("As configured");
    expect(verdictWord({ ...judged, status: "out" }, "uk")).toBe("Не за налаштуваннями");
    const named = row({ id: "failsafe", verdict: "named", values: { names: ["FAILSAFE_EKFINAV", "EKFCHECK"] } });
    expect(copingText(named, "en")).toBe("FAILSAFE_EKFINAV, EKFCHECK triggered");
  });

  it("falls back to the status's own word where a verdict has none", () => {
    expect(verdictWord(row({ id: "source", status: "info", verdict: "unknown" }), "en")).toBe("Info");
  });
});

describe("an event's facts", () => {
  it("gives its peaks and largest relative distances as stored", () => {
    expect(eventFacts(event(), "en")).toEqual([
      "fewest satellites 3",
      "accuracy as bad as 300 m",
      "GNSS up to 1,453 m from the estimator's position",
    ]);
    const time = event({ peaks: { lowest_agc_ratio: 0.62 }, position: null, time: { longest_time_stop_s: 2.4, largest_time_jump_s: 0 } });
    expect(eventFacts(time, "en")).toEqual([
      "AGC down to 62% of its usual level",
      "GPS time stood still up to 2.4 s and jumped up to 0 s",
    ]);
  });

  it("names its receivers, or the estimator's own checks", () => {
    expect(receiversLabel([1], "en")).toBe("GPS 1");
    expect(receiversLabel([1, 2], "en")).toBe("GPS 1 and GPS 2");
    expect(receiversLabel([1, 2], "uk")).toBe("GPS 1 і GPS 2");
    expect(receiversLabel([], "en")).toBe("the estimator's own checks");
  });

  it("gives times as seconds after arming", () => {
    expect(span(23.5, 59.2, "en")).toBe("23.5–59.2 s");
    expect(span(23.5, 59.2, "uk")).toBe("23,5–59,2 с");
  });
});

describe("readiness", () => {
  it("maps the review's statuses onto the one status language", () => {
    expect(AREA_LOOK.ready.status).toBe("ok");
    expect(AREA_LOOK.gap.status).toBe("caution");
    expect(AREA_LOOK.none.status).toBe("out");
    expect(AREA_LOOK.unavailable.status).toBe("info");
    expect(goalLook(false)).toEqual({ status: "out", label: "nav.goal.notMet" });
  });

  it("says each area as the analyzer does, filled from its values", () => {
    const flow = area({ status: "ready", say: "ready", values: { sets: [2], usable_sets: [2], sensors: ["optical_flow"] } });
    expect(areaText(flow, "en")).toBe("source set 2 keeps a position without GNSS (optical flow)");
    expect(areaText(flow, "uk")).toBe("набір джерел 2 утримує положення без GNSS (оптичний потік)");
    const sw = area({ area: "switching", say: "switch", fill: { channel: 8 } });
    expect(areaText(sw, "en")).toBe("RC8 selects the source set");
    const fs = area({ area: "failsafes", status: "ready", say: "ekf", values: { FS_EKF_ACTION: 1, dead_reckoning_failsafe_acts: false } });
    expect(areaText(fs, "en")).toBe("on losing its position the EKF failsafe lands it; the dead-reckoning failsafe never acts");
    const pilot = area({ area: "pilot_fallback", status: "ready", say: "modes", values: { modes: ["Stabilize"], drill_switch_channels: [7] } });
    expect(areaText(pilot, "en")).toBe("Stabilize on a switch; GPS-disable switch for drills on RC7");
    const heading = area({ area: "heading", status: "gap", say: "gap", fill: { sets: [1] } });
    expect(areaText(heading, "en")).toBe("source set 1 loses its heading with GNSS");
  });
});

describe("what would get it home", () => {
  it("gives each finding's own procedure steps to the shared step list", () => {
    const steps = actionSteps({
      finding: { id: "x", code: "no_gnss.drag_fusion_off", severity: "info", confidence: "high", params: {} },
      steps: [{ kind: "ground_check", text: { en: "Weigh it.", uk: "Зважте." } }],
    });
    expect(steps).toEqual([{ kind: "ground_check", text: { en: "Weigh it.", uk: "Зважте." }, params: [], step: null, checks: [] }]);
  });
});
