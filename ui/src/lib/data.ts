// What the views read from the engine more than once. The registry changes only with
// the knowledge pack, so it is read once per session.

import type { Registry } from "./report";
import { engineCall } from "./shell";

let registry: Promise<Registry> | null = null;

export function getRegistry(): Promise<Registry> {
  registry ??= engineCall<Registry>("analysis.registry").catch((e: unknown) => {
    registry = null; // ask again next time
    throw e;
  });
  return registry;
}
