import { describe, expect, it } from "vitest";
import {
  EMPTY_WORKER,
  ago,
  checksMatch,
  gapsByLaptop,
  laptopName,
  normalizeFingerprint,
  pickChecks,
  showValue,
  splitWords,
  syncPill,
} from "./sync";

describe("the header's sync pill", () => {
  it("is calm only when the last round was clean", () => {
    expect(syncPill({ ...EMPTY_WORKER, state: "idle" })).toEqual({ status: "ok", label: "sync.state.idle" });
    expect(syncPill({ ...EMPTY_WORKER, state: "idle", quarantined: 1 }).status).toBe("caution");
    expect(syncPill({ ...EMPTY_WORKER, state: "idle", gaps: [["ab", 3]] }).label).toBe("sync.state.gaps");
  });

  it("treats offline as a state, and a removal as the worst", () => {
    expect(syncPill({ ...EMPTY_WORKER, state: "offline" }).status).toBe("caution");
    expect(syncPill({ ...EMPTY_WORKER, state: "revoked" }).status).toBe("out");
    expect(syncPill({ ...EMPTY_WORKER, state: "" }).label).toBe("sync.state.starting");
    expect(syncPill(null).label).toBe("sync.state.browser");
  });
});

describe("time since the last sync", () => {
  const now = 1_800_000_000_000;
  it("uses the largest whole unit", () => {
    expect(ago(null, now).key).toBe("sync.ago.never");
    expect(ago(now - 5_000, now).key).toBe("sync.ago.now");
    expect(ago(now - 125_000, now)).toEqual({ key: "sync.ago.min", n: 2 });
    expect(ago(now - 3 * 3600_000, now)).toEqual({ key: "sync.ago.hour", n: 3 });
    expect(ago(now - 2 * 86400_000, now)).toEqual({ key: "sync.ago.day", n: 2 });
    expect(ago(now + 10_000, now).key).toBe("sync.ago.now"); // a clock ahead is not the future
  });
});

describe("fingerprints", () => {
  it("are read however they are typed", () => {
    expect(normalizeFingerprint("3f9a 2c11 0b7e")).toBe("3F9A-2C11-0B7E");
    expect(normalizeFingerprint(" 3F9A-2C11-0B7E ")).toBe("3F9A-2C11-0B7E");
    expect(normalizeFingerprint("3F9A-2C11")).toBeNull();
    expect(normalizeFingerprint("3F9A-2C11-0B7G")).toBeNull();
  });
});

describe("confirming the recovery words", () => {
  const words = splitWords(Array.from({ length: 24 }, (_, i) => `w${i}`).join("  "));

  it("asks for three distinct positions in order", () => {
    let k = 0;
    const seq = [0.5, 0.5, 0.1, 0.9]; // a repeat is skipped
    const checks = pickChecks(24, () => seq[k++] ?? 0);
    expect(checks).toEqual([2, 12, 21]);
    for (let i = 0; i < 50; i++) {
      const c = pickChecks(24);
      expect(new Set(c).size).toBe(3);
      expect(c).toEqual([...c].sort((a, b) => a - b));
    }
  });

  it("accepts only the right word at each position", () => {
    expect(words).toHaveLength(24);
    expect(checksMatch(words, [2, 12, 21], ["w2", " W12 ", "w21"])).toBe(true);
    expect(checksMatch(words, [2, 12, 21], ["w2", "w21", "w12"])).toBe(false);
    expect(checksMatch(words, [2, 12, 21], ["w2", "w12"])).toBe(false);
  });
});

describe("the sync panel", () => {
  const members = [{ id: "a1b2c3d4e5f6", name: "Bench", fingerprint: "X", revoked: false, me: true }];

  it("names laptops from the registry", () => {
    expect(laptopName("a1b2c3d4e5f6", members)).toBe("Bench");
    expect(laptopName("ffeeddccbbaa99", members)).toBe("LT-FFEEDD");
  });

  it("merges the drive's gaps with the engine's", () => {
    const gaps = gapsByLaptop(
      { ...EMPTY_WORKER, gaps: [["a", 4], ["b", 1]] },
      { waiting_to_send: 0, laptops: [{ laptop: "a", latest: 9, objects: 7, gaps: [4, 2] }], quarantine: [], conflicts: 0 },
    );
    expect(gaps.get("a")).toEqual([2, 4]);
    expect(gaps.get("b")).toEqual([1]);
  });

  it("shows both sides of a conflict as text", () => {
    expect(showValue("accepted")).toBe("accepted");
    expect(showValue(12.5)).toBe("12.5");
    expect(showValue(null)).toBe("—");
    expect(showValue({ a: 1 })).toBe('{"a":1}');
  });
});
