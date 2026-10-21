// The bridge to the Rust shell. The UI reaches the engine only through the
// shell's `engine_call`, which allows a fixed set of methods: never unlocking,
// never stopping, so keys never pass through the interface.

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export type EngineState = "starting" | "ready" | "failed" | "browser";

export interface ShellStatus {
  laptop: string | null;
  signing_public: string | null;
  encryption_public: string | null;
  first_start: boolean;
  engine: EngineState;
  engine_version: string | null;
  error: string | null;
}

export interface Progress {
  id: number;
  fraction: number;
  message: string;
}

/** True inside the desktop app; false in a plain browser (UI development). */
export function inDesktopApp(w: object = globalThis): boolean {
  return "__TAURI_INTERNALS__" in w;
}

export const BROWSER_STATUS: ShellStatus = {
  laptop: null,
  signing_public: null,
  encryption_public: null,
  first_start: false,
  engine: "browser",
  engine_version: null,
  error: null,
};

export async function shellStatus(): Promise<ShellStatus> {
  if (!inDesktopApp()) return BROWSER_STATUS;
  return invoke<ShellStatus>("shell_status");
}

export async function engineCall<T>(method: string, params: object = {}): Promise<T> {
  if (!inDesktopApp()) throw new Error("the engine runs only inside the desktop app");
  return invoke<T>("engine_call", { method, params });
}

/** Record this computer's facts on a ground station. The shell reads them; the
 * interface never sees the host name. */
export async function detectThisComputer<T>(id: string, version: number): Promise<T> {
  if (!inDesktopApp()) throw new Error("detection runs only inside the desktop app");
  return invoke<T>("detect_this_computer", { id, version });
}

/** A new file found in a watched save folder, waiting to be offered for import. */
export interface PendingFile {
  path: string;
  name: string;
  size: number;
  station_id: string;
  station: string;
  station_version_id: string | null;
}

/** Files found in watched folders. With `refresh`, the engine is asked again first. */
export async function pendingImports(refresh = false): Promise<PendingFile[]> {
  if (!inDesktopApp()) return [];
  return invoke<PendingFile[]>("pending_imports", { refresh });
}

export async function onPendingImports(handler: (files: PendingFile[]) => void): Promise<UnlistenFn> {
  if (!inDesktopApp()) return () => {};
  return listen<PendingFile[]>("import-pending", (e) => handler(e.payload));
}

export async function onShellStatus(handler: (s: ShellStatus) => void): Promise<UnlistenFn> {
  if (!inDesktopApp()) return () => {};
  return listen<ShellStatus>("shell-status", (e) => handler(e.payload));
}

export async function onProgress(handler: (p: Progress) => void): Promise<UnlistenFn> {
  if (!inDesktopApp()) return () => {};
  return listen<Progress>("engine-progress", (e) => handler(e.payload));
}
