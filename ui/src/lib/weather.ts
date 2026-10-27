// A flight's weather, as the engine's flight.weather returns it, and the downloader's
// state, as the shell's weather_status gives it, with the helpers behind their screens
// (spec: Weather and wind; mock-ups Weather and SettingsWeather). The record is worked
// out on this laptop from whole regional tiles and holds values only: no tile, no
// coordinate. Nothing here works out a value; it only lays them out.

import { invoke } from "@tauri-apps/api/core";
import type { MessageKey } from "./i18n";
import { engineCall, inDesktopApp } from "./shell";
import type { Status } from "./status";

export type Quality = "forecast" | "reanalysis" | "mixed" | "none";

/** The record (engine weather/record.py): mean and largest wind at the flight's height,
 * where it blows from, the 10 m wind and gusts, take-off conditions, and the K-index. */
export interface WeatherRecord {
  quality: Quality;
  coverage: number; // the share of the flight's samples a download covered
  height_band_m?: [number, number]; // above take-off
  wind_ms?: number | null;
  wind_max_ms?: number | null;
  wind_from_deg?: number | null;
  surface_wind_ms?: number | null;
  gust_max_ms?: number | null;
  precipitation_mm_h?: number | null;
  cloud_cover_pct?: number | null;
  boundary_layer_m?: number | null;
  temperature_c?: number;
  pressure_hpa?: number;
  humidity_pct?: number | null;
  dew_point_c?: number;
  density_altitude_m?: number;
  density_altitude_top_m?: number;
  cloud_base_m?: number;
  kp_max: number | null;
}

/** The wind the aircraft measured: from its own flight, or from its airframe's drag
 * (`drag_from`: the flights it came from) when it only hovered. */
export interface MeasuredWind {
  source: "flight" | "airframe";
  speed_ms: number;
  peak_ms: number;
  from_deg: number;
  windows: number;
  height_m: number;
  drag_from: string[];
}

/** The model less the measured: speed, and the model's direction turned from the
 * measured (given only when the measured wind is over 0.5 m/s). */
export interface WindGap {
  speed_ms: number;
  from_deg?: number;
}

export interface FlightWeather {
  record: WeatherRecord | null; // null: no UTC time or no position in the log
  measured: MeasuredWind | null;
  difference: WindGap | null;
  kp_elevated: number; // NOAA's warning level, from the rule pack
}

export function flightWeather(flightId: string): Promise<FlightWeather> {
  return engineCall<FlightWeather>("flight.weather", { flight_id: flightId });
}

/** Whether a download covered any of the flight. */
export function covered(rec: WeatherRecord | null): rec is WeatherRecord {
  return !!rec && rec.quality !== "none" && rec.coverage > 0;
}

/** The model a record came from, by name: the forecast is ECMWF IFS HRES, the
 * reanalysis ERA5 (spec: Weather and wind, the sources table). */
export function modelName(quality: Quality): string {
  switch (quality) {
    case "forecast":
      return "ECMWF IFS HRES";
    case "reanalysis":
      return "ERA5";
    case "mixed":
      return "ERA5 + ECMWF IFS HRES";
    default:
      return "–";
  }
}

export type KpLevel = "unknown" | "quiet" | "elevated";

export function kpLevel(kp: number | null, elevated: number): KpLevel {
  if (kp === null || !Number.isFinite(kp)) return "unknown";
  return kp >= elevated ? "elevated" : "quiet";
}

/** A number for this screen, with the precision its value carries, or a dash. */
export function num(value: number | null | undefined, digits: number, locale: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "–";
  return new Intl.NumberFormat(locale === "uk" ? "uk-UA" : "en-GB", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value);
}

/** A direction the wind blows from, in whole degrees true, 0 to 359. */
export function bearing(deg: number | null | undefined): string {
  if (deg === null || deg === undefined || !Number.isFinite(deg)) return "–";
  return `${((Math.round(deg) % 360) + 360) % 360}°`;
}

/** A signed difference: "+1.2" or "−0.3" (a true minus sign), "0" when none. */
export function signed(value: number, digits: number, locale: string): string {
  const text = num(Math.abs(value), digits, locale);
  if (Number(Math.abs(value).toFixed(digits)) === 0) return text;
  return `${value < 0 ? "−" : "+"}${text}`;
}

/** The flight's height band above take-off, in whole metres. */
export function band(rec: WeatherRecord, locale: string): string | null {
  if (!rec.height_band_m) return null;
  const [lo, hi] = rec.height_band_m;
  return `${num(lo, 0, locale)}–${num(hi, 0, locale)}`;
}

// ---------------------------------------------------------------------------------
// The downloader (shell weather_status): never a tile, a place or the key.

export interface WeatherStatus {
  state: string; // waiting, done, offline, key refused, busy, failed
  key_set: boolean;
  next_ms: number | null;
  last_ms?: number;
  tiles?: number;
  kept?: number;
  from_fleet?: number;
  shared?: number;
  why?: string;
}

export async function weatherStatus(): Promise<WeatherStatus | null> {
  if (!inDesktopApp()) return null;
  return invoke<WeatherStatus>("weather_status");
}

export async function weatherKeySet(key: string): Promise<void> {
  return invoke("weather_key_set", { key });
}

export async function weatherKeyClear(): Promise<void> {
  return invoke("weather_key_clear");
}

/** How the last round reads: its status colour and its words. */
export function roundLook(s: WeatherStatus): { status: Status; label: MessageKey } {
  if (!s.key_set) return { status: "info", label: "wx.state.nokey" };
  switch (s.state) {
    case "done":
      return { status: "ok", label: "wx.state.done" };
    case "waiting":
      return { status: "info", label: "wx.state.waiting" };
    case "offline":
      return { status: "caution", label: "wx.state.offline" };
    case "busy":
      return { status: "caution", label: "wx.state.busy" };
    case "key refused":
      return { status: "out", label: "wx.state.refused" };
    default:
      return { status: "out", label: "wx.state.failed" };
  }
}

/** A schedule instant, in UTC as the schedule is kept: "27 Oct, 12:23 UTC". */
export function utcInstant(ms: number | null | undefined, locale: string): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "–";
  const text = new Intl.DateTimeFormat(locale === "uk" ? "uk-UA" : "en-GB", {
    timeZone: "UTC",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(ms));
  return `${text} UTC`;
}
