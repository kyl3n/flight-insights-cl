// The printable range card (spec: Power and efficiency analysis, "Range, endurance and
// speed"; mock-up RangeCard): a row per take-off weight with how long, how far and how
// fast, the radius for each standard trip, the ground speed to set into a headwind, and
// what the card rests on. The engine works every figure out on this laptop; the low end
// of each interval is printed. No position is on it.
import type { Air, Failsafe, Trip } from "./range";
import { engineCall } from "./shell";

export interface CardRow {
  mass_kg: number;
  hovers: boolean;
  measured?: boolean;
  hover_min?: number;
  endurance_min?: number;
  endurance_ms?: number;
  cruise_ms?: number;
  cruise_for?: "range" | "endurance";
  range_km?: number;
  radius_km?: Record<string, number>;
  radius_still_low_km?: number;
  radius_trips_km?: Record<Trip, Record<string, number>>;
  headwind?: { wind_ms: number; airspeed: number | null; ground: number | null }[];
  plan_wh?: number;
  top_speed_ms?: number;
  top_speed_by?: string;
  verified_ms?: number | null;
}

export interface RangeCard {
  airframe_id: string;
  missing: Record<string, string>;
  sources: Record<string, string>;
  rows: CardRow[];
  valid?: { flown_kg: [number, number] | null; top_ms: number | null; density: number; reserve_share: number; params_flight: string | null };
  card?: {
    air: Air;
    density: number;
    empty_kg: number | null;
    priority: string | null;
    failsafe: Failsafe;
    params: Record<string, number | null>;
    model: { level: string | null; flights: number; lofo_median: number | null; lofo_p90: number | null; targets: { median: number; p90: number } };
    trips: Trip[];
    winds: number[];
  };
}

export function rangeCard(airframeId: string, air: Air): Promise<RangeCard> {
  return engineCall<RangeCard>("airframe.range_card", { airframe_id: airframeId, air });
}

/** Whether the model meets its accuracy targets: each flight's energy predicted from the
 * others within the median and 90th-percentile targets. Null while it cannot say. Meeting
 * them is not validation: that also needs a flight to the reserve (spec, Verification). */
export function meetsTargets(card: RangeCard): boolean | null {
  const m = card.card?.model;
  if (!m || m.lofo_median === null || m.lofo_p90 === null) return null;
  return Math.abs(m.lofo_median) <= m.targets.median && Math.abs(m.lofo_p90) <= m.targets.p90;
}

/** The radius for a trip in a wind, km, or null where the card has none. */
export function radiusAt(row: CardRow, trip: Trip, wind: number): number | null {
  const byWind = row.radius_trips_km?.[trip];
  if (!byWind) return null;
  const key = Object.keys(byWind).find((k) => Math.abs(Number(k) - wind) < 1e-9);
  return key === undefined ? null : (byWind[key] ?? null);
}
