// What the views read from the engine more than once. The registry changes only with
// the knowledge pack, so it is read once per session.

import type { FlightRow, Registry } from "./report";
import { engineCall } from "./shell";

let registry: Promise<Registry> | null = null;

export function getRegistry(): Promise<Registry> {
  registry ??= engineCall<Registry>("analysis.registry").catch((e: unknown) => {
    registry = null; // ask again next time
    throw e;
  });
  return registry;
}

/** An airframe's latest flight, the one its plan and review are built from. */
export async function latestFlight(airframeId: string): Promise<FlightRow | null> {
  const rows = await engineCall<FlightRow[]>("flight.list", { airframe_id: airframeId });
  return rows[0] ?? null;
}
