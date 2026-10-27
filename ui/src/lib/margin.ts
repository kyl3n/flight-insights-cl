// A flight's motor-out margin and payload, as the engine's flight.margin returns them, and
// the helpers behind their panels in the Propulsion view (spec: Propulsion, "Motor-out
// margin" and "Payload"; mock-up Propulsion). The numbers are the analyzers'; here they
// are only laid out, each motor placed where ArduPilot's mixer factors put it.

import type { MessageKey } from "./i18n";
import { engineCall } from "./shell";
import type { Status } from "./status";

export interface MotorSlot {
  motor: number;
  roll: number; // the mixer's factors, normalised to 0.5 at most
  pitch: number;
  yaw: number;
  needed_pct: number | null; // the busiest other motor's output with this one stopped
}

export interface MotorOut {
  frame: string; // "OCTAQUAD/X"
  hold_yaw: boolean;
  target_pct: number;
  hover_thrust_pct: number;
  worst_motor: number;
  busiest_motor?: number;
  worst_output_pct?: number;
  best_split_output_pct?: number | null;
  margin_pts: number | null;
  weight_ratio: number | null;
  heaviest_kg: number | null;
  weight_from: "weighed" | "estimated" | null;
  motors: MotorSlot[];
  finding: { code: string; severity: string } | null;
}

export interface Payload {
  hover_thrust_pct: number | null;
  seconds: number | null;
  tilt_deg: number | null;
  density_ratio: number | null;
  group: string | null;
  estimated_kg: number | null;
  takeoff_kg: number | null;
}

export type Skipped = { skipped: string | null };

export interface MarginView {
  airframe: string | null;
  payload: Payload | Skipped | null;
  motor_out: MotorOut | Skipped | null;
}

export function flightMargin(flightId: string): Promise<MarginView> {
  return engineCall<MarginView>("flight.margin", { flight_id: flightId });
}

export function isSkipped<T extends object>(x: T | Skipped | null): x is Skipped {
  return !!x && "skipped" in x;
}

export type Side =
  | "front"
  | "frontRight"
  | "right"
  | "rearRight"
  | "rear"
  | "rearLeft"
  | "left"
  | "frontLeft";

const SIDES: Side[] = ["front", "frontRight", "right", "rearRight", "rear", "rearLeft", "left", "frontLeft"];

/** Where a motor sits, clockwise from the nose in degrees: a motor that rolls the
 * aircraft right when it speeds up (a positive roll factor) is on the left, and one that
 * pitches the nose up is at the front. */
export function bearing(m: Pick<MotorSlot, "roll" | "pitch">): number {
  const deg = (Math.atan2(-m.roll, m.pitch) * 180) / Math.PI;
  return (deg + 360) % 360;
}

export function side(m: Pick<MotorSlot, "roll" | "pitch">): Side {
  return SIDES[Math.round(bearing(m) / 45) % 8] ?? "front";
}

export interface Station {
  bearing: number;
  x: number; // -1 (left) to 1 (right)
  y: number; // -1 (front) to 1 (rear), as drawn
  motors: number[]; // more than one on a coaxial frame, top and bottom
}

/** The frame's arms: motors at the same place (a coaxial pair) share one. */
export function stations(motors: MotorSlot[]): Station[] {
  const out: Station[] = [];
  for (const m of [...motors].sort((a, b) => a.motor - b.motor)) {
    const b = bearing(m);
    const at = out.find((s) => Math.abs(((s.bearing - b + 540) % 360) - 180) < 1);
    if (at) at.motors.push(m.motor);
    else {
      const r = (b * Math.PI) / 180;
      out.push({ bearing: b, x: Math.sin(r), y: -Math.cos(r), motors: [m.motor] });
    }
  }
  return out;
}

/** The motor that shares a stopped motor's arm, if any. */
export function partners(motors: MotorSlot[], motor: number): number[] {
  return stations(motors).find((s) => s.motors.includes(motor))?.motors.filter((n) => n !== motor) ?? [];
}

/** How one motor's loss reads against the target: met, over it, or no balance at all. */
export function lossLook(needed: number | null, target: number): { status: Status; label: MessageKey } {
  if (needed === null) return { status: "out", label: "margin.loss.noBalance" };
  return needed <= target ? { status: "ok", label: "margin.loss.meets" } : { status: "caution", label: "margin.loss.over" };
}

/** A bar's fill for an output, 0 to 100% of its width. */
export function fill(pct: number | null): number {
  if (pct === null || !Number.isFinite(pct)) return 0;
  return Math.max(0, Math.min(100, pct));
}
