import { describe, expect, it } from "vitest";
import { byUrgency, findingCount, keep, stagePosition, statusLook, type AirframeHealth } from "./fleet";

const H: Record<string, AirframeHealth> = {
  a: { status: "ok", flights: 4, stage: null, counts: { info: 2 } },
  b: { status: "fix", flights: 2, stage: "filters", counts: { out: 1, caution: 2 } },
  c: { status: "inspect", flights: 9, stage: null },
  d: { status: "none", flights: 0 },
  e: { status: "ok", flights: 3, stage: "rate tune" },
};

describe("fleet", () => {
  it("filters airframes the way the overview's buttons say", () => {
    const ids = (f: Parameters<typeof keep>[0]) => Object.keys(H).filter((id) => keep(f, H[id]));
    expect(ids("all")).toEqual(["a", "b", "c", "d", "e"]);
    expect(ids("attention")).toEqual(["b", "c"]);
    expect(ids("tuning")).toEqual(["b", "e"]);
    expect(ids("healthy")).toEqual(["a"]);
    expect(keep("all", undefined)).toBe(true);
  });

  it("orders airframes most urgent first, then by name", () => {
    const rows = ["d", "a", "e", "c", "b"].map((id) => ({ id, name: `HL-${id}` }));
    expect(byUrgency(rows, H).map((r) => r.id)).toEqual(["b", "c", "a", "e", "d"]);
  });

  it("counts what needs a look and places the stage", () => {
    expect(findingCount(H.b!)).toBe(3);
    expect(findingCount(H.a!)).toBe(0);
    expect(stagePosition("filters")).toEqual({ at: 4, of: 9 });
    expect(stagePosition("nonsense")).toBeNull();
    expect(statusLook("fix").status).toBe("out");
    expect(statusLook("none").label).toBe("fleet.status.none");
  });
});
