import { describe, expect, it } from "vitest";
import {
  band,
  bearing,
  covered,
  kpLevel,
  modelName,
  num,
  roundLook,
  signed,
  utcInstant,
  type WeatherRecord,
  type WeatherStatus,
} from "./weather";

const record: WeatherRecord = {
  quality: "forecast",
  coverage: 1,
  height_band_m: [9.6, 15.2],
  wind_ms: 5.21,
  wind_from_deg: 254.6,
  kp_max: 2.33,
};

describe("the flight's record", () => {
  it("counts only a record a download covered", () => {
    expect(covered(record)).toBe(true);
    expect(covered(null)).toBe(false);
    expect(covered({ quality: "none", coverage: 0, kp_max: null })).toBe(false);
    expect(covered({ ...record, coverage: 0 })).toBe(false);
  });

  it("names the model each grade came from", () => {
    expect(modelName("forecast")).toBe("ECMWF IFS HRES");
    expect(modelName("reanalysis")).toBe("ERA5");
    expect(modelName("mixed")).toBe("ERA5 + ECMWF IFS HRES");
    expect(modelName("none")).toBe("–");
  });

  it("marks the K-index elevated from NOAA's warning level", () => {
    expect(kpLevel(2.33, 4)).toBe("quiet");
    expect(kpLevel(4, 4)).toBe("elevated");
    expect(kpLevel(5.67, 4)).toBe("elevated");
    expect(kpLevel(null, 4)).toBe("unknown");
  });

  it("writes numbers at the precision they carry, a dash for none", () => {
    expect(num(5.21, 1, "en")).toBe("5.2");
    expect(num(1400, 0, "en")).toBe("1,400");
    expect(num(5.21, 1, "uk")).toBe("5,2");
    expect(num(null, 1, "en")).toBe("–");
    expect(num(undefined, 1, "en")).toBe("–");
    expect(num(Number.NaN, 1, "en")).toBe("–");
  });

  it("writes where the wind blows from in whole degrees, 0 to 359", () => {
    expect(bearing(254.6)).toBe("255°");
    expect(bearing(359.7)).toBe("0°");
    expect(bearing(-10)).toBe("350°");
    expect(bearing(null)).toBe("–");
  });

  it("signs a difference with a true minus, and none for zero", () => {
    expect(signed(-0.34, 1, "en")).toBe("−0.3");
    expect(signed(1.2, 1, "en")).toBe("+1.2");
    expect(signed(0.02, 1, "en")).toBe("0.0");
    expect(signed(-20, 0, "en")).toBe("−20");
  });

  it("gives the height band in whole metres", () => {
    expect(band(record, "en")).toBe("10–15");
    expect(band({ ...record, height_band_m: undefined }, "en")).toBeNull();
  });
});

describe("the downloader's state", () => {
  const status = (s: Partial<WeatherStatus>): WeatherStatus => ({
    state: "done",
    key_set: true,
    next_ms: null,
    ...s,
  });

  it("asks for a key before anything else", () => {
    expect(roundLook(status({ key_set: false, state: "failed" }))).toEqual({
      status: "info",
      label: "wx.state.nokey",
    });
  });

  it("reads each round's outcome", () => {
    expect(roundLook(status({})).status).toBe("ok");
    expect(roundLook(status({ state: "waiting" })).status).toBe("info");
    expect(roundLook(status({ state: "offline" })).label).toBe("wx.state.offline");
    expect(roundLook(status({ state: "busy" })).status).toBe("caution");
    expect(roundLook(status({ state: "key refused" }))).toEqual({
      status: "out",
      label: "wx.state.refused",
    });
    expect(roundLook(status({ state: "plan" }))).toEqual({ status: "out", label: "wx.state.plan" });
    expect(roundLook(status({ state: "something new" })).label).toBe("wx.state.failed");
  });

  it("gives schedule instants in UTC, as the schedule keeps them", () => {
    const ms = Date.UTC(2026, 9, 27, 12, 23);
    expect(utcInstant(ms, "en")).toBe("27 Oct, 12:23 UTC");
    expect(utcInstant(null, "en")).toBe("–");
  });
});
