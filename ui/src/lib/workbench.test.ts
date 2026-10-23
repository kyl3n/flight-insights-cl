import { describe, expect, it } from "vitest";
import { changes, colour, dbRange, delta, pixels, plotData, type Bench, type Evaluation } from "./workbench";

const ev = (over: Partial<Evaluation> = {}): Evaluation => ({
  available: true, noise_db: -31.1, lag_deg: 93.5, notches: 2, within_budget: true,
  centres_hz: [201.8], bandwidth_hz: 90, lowpass_hz: 20,
  spectrum_db: [[1, 2], [3, 4], [5, 6]], ...over,
});

const BENCH: Bench = {
  segment: "hover", spans_s: [[8.8, 22.6]], seconds: 13.3, rate_hz: 1000, instance: 0,
  noise_band_hz: [40, 450], lag_hz: 20, notch_cap: 54, tracking: [], rpm_reliable: true,
  freqs: [0, 1], before_db: [[10, 11], [12, 13], [14, 15]], flown_db: null, flown_from: null,
  params: { INS_HNTCH_BW: 90, INS_GYRO_FILTER: 20 }, current: ev(), spectrogram: null,
};

describe("workbench", () => {
  it("sends only what the person changed", () => {
    expect(changes(BENCH.params, { INS_HNTCH_BW: "40", INS_GYRO_FILTER: "20", INS_HNTCH_REF: " " })).toEqual({
      INS_HNTCH_BW: 40,
    });
    expect(changes(BENCH.params, { INS_GYRO_FILTER: "30,5", INS_HNTCH_BW: "wide" })).toEqual({ INS_GYRO_FILTER: 30.5 });
  });

  it("lays out one axis for the plot, with gaps where a series is missing", () => {
    expect(plotData(BENCH, "pitch")).toEqual([[0, 1], [12, 13], [null, null], [null, null]]);
    const withCandidate = { ...BENCH, candidate: ev({ spectrum_db: [[0, 0], [7, 8], [0, 0]] }) };
    expect(plotData(withCandidate, "pitch")[3]).toEqual([7, 8]);
  });

  it("reports the candidate's change in noise and lag", () => {
    expect(delta(BENCH)).toBeNull();
    expect(delta({ ...BENCH, candidate: ev({ noise_db: -20, lag_deg: 60.8 }) })).toEqual({ noise_db: 11.1, lag_deg: -32.7 });
    expect(delta({ ...BENCH, candidate: ev({ available: false }) })).toBeNull();
  });

  it("maps the spectrogram to colours, frequency up", () => {
    expect(dbRange([[0, 10], [5, 100]])[1]).toBeGreaterThan(5);
    expect(colour(0)).toEqual([12, 18, 32]);
    expect(colour(1)).toEqual([250, 246, 230]);
    const img = pixels({ t_s: [0, 1], freqs: [0, 100], db: [[-60, 0], [-60, 0]] });
    expect([img.width, img.height, img.data.length]).toEqual([2, 2, 16]);
    expect(Array.from(img.data.slice(0, 3))).toEqual([250, 246, 230]); // top row: 100 Hz, loud
    expect(Array.from(img.data.slice(8, 11))).toEqual([12, 18, 32]); // bottom row: 0 Hz, quiet
  });
});
