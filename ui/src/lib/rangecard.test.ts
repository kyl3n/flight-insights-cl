import { describe, expect, it } from "vitest";
import { radiusAt, meetsTargets, type RangeCard } from "./rangecard";

const card = (median: number | null, p90: number | null): RangeCard => ({
  airframe_id: "a",
  missing: {},
  sources: {},
  rows: [],
  card: {
    air: "usual",
    density: 1.16,
    empty_kg: 30,
    priority: null,
    failsafe: { low_volt: 42, timer_s: 10, resting: false, capacity_mah: 30000, low_mah: 4400, current_limit_a: null },
    params: {},
    model: { level: "A", flights: 6, lofo_median: median, lofo_p90: p90, targets: { median: 0.05, p90: 0.1 } },
    trips: ["out_and_back", "drop", "site"],
    winds: [0, 5, 10],
  },
});

describe("range card", () => {
  it("says the model is meetsTargets only when both targets are met", () => {
    expect(meetsTargets(card(0.04, 0.09))).toBe(true);
    expect(meetsTargets(card(-0.04, 0.12))).toBe(false);
    expect(meetsTargets(card(null, null))).toBeNull();
  });

  it("reads a trip's radius in a wind whatever the key's spelling", () => {
    const row = { mass_kg: 30, hovers: true, radius_trips_km: { out_and_back: { "0.0": 4, "5.0": 3 }, drop: {}, site: {} } };
    expect(radiusAt(row, "out_and_back", 5)).toBe(3);
    expect(radiusAt(row, "drop", 5)).toBeNull();
    expect(radiusAt({ mass_kg: 30, hovers: false }, "site", 0)).toBeNull();
  });
});
