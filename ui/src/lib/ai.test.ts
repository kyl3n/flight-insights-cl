import { describe, expect, it } from "vitest";
import {
  findingFor,
  fromAi,
  hlcTime,
  prettyPacket,
  sectionState,
  verdictCounts,
  type AiStatus,
  type Exchange,
} from "./ai";

const status: AiStatus = {
  key_set: true, key_rejected: false, monthly_cap_usd: 20, language: "en",
  month: { month: "2026-10", cost_usd: 1.2, exchanges: 3 }, queued: 0, gate: "ready",
};
const exchange: Exchange = {
  id: "x1", packet_id: "p1", flight_id: "f1", model: "m", outcome: "accepted",
  reasons: [], recommendations: [], tool_calls: 2, cost_usd: 0.3, created: "1792826368964-0-ab",
  refs: { F1: "finding-a", E1: "ev-1" },
  reply: { summary: "s", root_causes: [], recommendations: [], disagreements: [], questions: [],
           tests_to_fly: [] },
};

describe("the AI section", () => {
  it("says why it is off before anything else", () => {
    expect(sectionState(null, exchange, null)).toEqual({ kind: "off", why: "browser" });
    expect(sectionState({ ...status, gate: "blocked" }, exchange, null).kind).toBe("off");
    expect(sectionState({ ...status, key_set: false }, null, null)).toEqual({ kind: "off", why: "no_key" });
    expect(sectionState({ ...status, key_rejected: true }, null, null)).toEqual({
      kind: "off", why: "key_rejected",
    });
  });

  it("shows an accepted reply, a problem, or that it is waiting", () => {
    expect(sectionState(status, exchange, null).kind).toBe("shown");
    expect(sectionState(status, { ...exchange, outcome: "declined", reply: null }, null).kind).toBe("problem");
    expect(sectionState(status, null, null).kind).toBe("pending");
    const sending = { flight_id: "f1", state: "sending" as const, detail: null };
    expect(sectionState(status, exchange, sending).kind).toBe("pending");
    expect(sectionState(status, null, { ...sending, state: "held" }).kind).toBe("held");
  });

  it("opens findings, not evidence, from references", () => {
    expect(findingFor(exchange, "F1")).toBe("finding-a");
    expect(findingFor(exchange, "E1")).toBeNull();
    expect(findingFor(exchange, "F9")).toBeNull();
  });

  it("counts how the proposed changes fared", () => {
    const v = (status: string) => ({ param: "X", value: 1, status, notes: [] });
    expect(verdictCounts([v("next"), v("waiting"), v("blocked"), v("refused"), v("unchanged")]))
      .toEqual({ planned: 2, refused: 2, unchanged: 1 });
  });

  it("reads clock stamps, lays packets out and spots AI recommendations", () => {
    expect(hlcTime("1792826368964-0-ab")?.getTime()).toBe(1792826368964);
    expect(hlcTime(null)).toBeNull();
    expect(prettyPacket('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(prettyPacket("not json")).toBe("not json");
    expect(fromAi({ sources: [{ origin: "ai" }] })).toBe(true);
    expect(fromAi({ sources: [{ origin: "measured" }] })).toBe(false);
  });
});
