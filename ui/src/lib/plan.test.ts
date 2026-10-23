import { describe, expect, it } from "vitest";
import {
  decisionCounts,
  groupByFinding,
  groupDecision,
  outcomes,
  partition,
  recFor,
  stageStates,
  testCard,
  type HistoryRow,
  type Plan,
  type PlanRec,
} from "./plan";

const t = (en: string) => ({ en, uk: `uk ${en}` });

function rec(over: Partial<PlanRec>): PlanRec {
  return {
    param: "INS_GYRO_FILTER", canonical: "INS_GYRO_FILTER", finding: "f1", current: 20,
    proposed: 25, target: null, stage: "filters", kind: "stability", status: "next", step: 1,
    why: t("why"), evidence: [], expected_effect: { ...t("effect"), simulated: true },
    confidence: "high", risk: "medium", requires: [], reboot: false, verify: t("hover"),
    source: "finding:x", sources: [], notes: [], alternatives: [], ...over,
  };
}

const PLAN: Plan = {
  flight: "fl", vehicle: "copter", firmware: "4.7.0", stage: "filters", steps: [],
  recommendations: [
    rec({ record_status: "accepted" }),
    rec({ param: "INS_HNTCH_MODE", canonical: "INS_HNTCH_MODE", finding: "f2" }),
    rec({ param: null, canonical: null, finding: "f3", stage: "mechanical", why: t("check motor 3"), verify: t("hover") }),
    rec({ param: "ATC_RAT_RLL_P", status: "waiting", stage: "rate tune", verify: t("steps") }),
    rec({ param: "ARMING_SKIPCHK", status: "blocked", stage: "operations" }),
    rec({ param: "MOT_PWM_TYPE", status: "disputed", finding: "f4", sources: [
      { finding: "f9", analyzer: "options", code: "option.x", severity: "info", why: t("w") },
    ] }),
  ],
};

describe("plan", () => {
  it("marks stages clear before the one on offer and waiting after it", () => {
    const states = stageStates(PLAN);
    expect(states.find((s) => s.stage === "setup")?.state).toBe("clear");
    expect(states.find((s) => s.stage === "filters")).toEqual({ stage: "filters", state: "now", count: 3 });
    expect(states.find((s) => s.stage === "operations")).toEqual({ stage: "operations", state: "later", count: 0 });
    expect(stageStates({ ...PLAN, stage: null }).every((s) => s.state === "clear")).toBe(true);
  });

  it("splits what is on offer, waiting and blocked; disputed needs a person now", () => {
    const p = partition(PLAN);
    expect(p.now.map((r) => r.param)).toEqual(["INS_GYRO_FILTER", "INS_HNTCH_MODE", null, "MOT_PWM_TYPE"]);
    expect(p.waiting.map((r) => r.param)).toEqual(["ATC_RAT_RLL_P"]);
    expect(p.blocked.map((r) => r.param)).toEqual(["ARMING_SKIPCHK"]);
  });

  it("counts decisions on parameter changes only", () => {
    expect(decisionCounts(PLAN)).toEqual({ accepted: 1, open: 1 });
  });

  it("builds the test flight card: actions first, each check once", () => {
    expect(testCard(PLAN, "en")).toEqual({ first: ["check motor 3"], checks: ["hover"] });
    expect(testCard(PLAN, "uk").first).toEqual(["uk check motor 3"]);
  });

  it("finds the plan's recommendation for a finding, as lead or as a source", () => {
    expect(recFor(PLAN, "f2")?.param).toBe("INS_HNTCH_MODE");
    expect(recFor(PLAN, "f9")?.param).toBe("MOT_PWM_TYPE");
    expect(recFor(PLAN, "nope")).toBeUndefined();
  });

  it("lists the latest graded outcomes", () => {
    const row = (id: string, status: HistoryRow["status"], when: string): HistoryRow => ({
      id, param: "P", proposed_value: 1, status, flight_id: "a", graded_flight_id: "b",
      updated_hlc: when, detail: {},
    });
    const list = outcomes([row("1", "confirmed", "01"), row("2", "proposed", "03"), row("3", "regressed", "02")]);
    expect(list.map((h) => h.id)).toEqual(["3", "1"]);
  });

  it("groups the changes one finding proposes into one card", () => {
    const recs = [
      rec({ param: "A", finding: "f1", id: "1", record_status: "accepted" }),
      rec({ param: null, finding: "f3" }),
      rec({ param: "B", finding: "f1", id: "2", record_status: "proposed" }),
      rec({ param: "C", finding: "f2", id: "3" }),
    ];
    const groups = groupByFinding(recs);
    expect(groups.map((g) => g.recs.map((r) => r.param))).toEqual([["A", "B"], [null], ["C"]]);
    expect(groupDecision(groups[0]!)).toBe("mixed");
    expect(groupDecision(groups[2]!)).toBe("proposed");
    expect(groupDecision(groups[1]!)).toBe("none");
  });
});
