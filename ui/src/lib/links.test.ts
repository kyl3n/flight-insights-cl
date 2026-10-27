import { describe, expect, it } from "vitest";
import { CATALOGS, type MessageKey } from "./i18n";
import {
  delayStation,
  linkFindings,
  linkMetrics,
  longestSpan,
  metricNote,
  radioRows,
  settingKey,
  valueNote,
  verdictNote,
  worstLoss,
  type Loss,
  type SettingRow,
  type StationView,
} from "./links";
import type { EvidenceRow, FindingRow, MetricValue } from "./report";

const fmt = (v: number) => String(v);
const metric = (id: string, segment = "flight"): MetricValue => ({
  metric: id, segment, value: 1, spread: {}, zone: "target", evidence: `ev:${id}`,
});
const finding = (code: string, severity: FindingRow["severity"], evidence: string[] = []): FindingRow => ({
  id: code, analyzer: "x", code, severity, confidence: "high", params: {}, evidence, changes: [], stage: null,
});
const ev = (id: string, values: Record<string, unknown>): EvidenceRow => ({
  id, kind: "fact", summary: "", messages: [], fields: [], segment: null, values,
});
const row = (over: Partial<SettingRow>): SettingRow => ({ param: "FS_THR_ENABLE", value: 1, status: "ok", verdict: "on", ...over });
const station = (n: number, delay: [number, number] | null): StationView => ({
  station: n, received: 10, lost: 0, out_of_order: 0, loss_pct: 0, longest_s: 0.3, longest_at_s: 1,
  outages: 0, clock: "unconfirmed", delay_ms: delay, silence: { t: [], lo: [], hi: [] }, outage_list: [],
});

describe("the links view", () => {
  it("shows the link measures of the whole flight in screen order", () => {
    const metrics = [metric("link.rc_lq"), metric("power.sag"), metric("link.downlink_loss"),
      metric("link.new_one"), metric("link.uplink_loss", "hover")];
    expect(linkMetrics({ metrics }).map((m) => m.metric)).toEqual(["link.downlink_loss", "link.rc_lq", "link.new_one"]);
  });

  it("picks the findings on the links, the link failsafes among them, most urgent first", () => {
    const findings = [
      finding("link.downlink_loss", "caution"),
      finding("failsafe.as_configured", "info", ["fs:gcs"]),
      finding("failsafe.as_configured", "info", ["fs:batt"]),
      finding("command.unanswered", "out"),
      finding("operator.unanswered_warning", "caution"),
      finding("vibration.clipping", "out"),
    ];
    findings[2]!.id = "batt";
    const evidence = [ev("fs:gcs", { failsafe: "FAILSAFE_GCS" }), ev("fs:batt", { failsafe: "FAILSAFE_BATT" })];
    const picked = linkFindings({ findings, evidence });
    expect(picked.map((f) => f.code)).toEqual([
      "command.unanswered", "link.downlink_loss", "operator.unanswered_warning", "failsafe.as_configured",
    ]);
    expect(picked.map((f) => f.id)).not.toContain("batt");
  });

  it("words the line under a measure from its evidence, or not at all", () => {
    const down = ev("link:downlink", { lost: 3, received: 97, outages: 1, longest_at_s: 12.5 });
    expect(metricNote("link.downlink_loss", down, fmt)).toEqual({ key: "links.note.downlink", vars: { lost: "3", total: "100" } });
    expect(metricNote("link.downlink_outage", down, fmt)).toEqual({ key: "links.note.outage", vars: { outages: "1", longest_at_s: "12.5" } });
    const silence = ev("link:gcs_silence", { longest_s: 3, timeout_s: 5, from_s: 20 });
    expect(metricNote("link.gcs_silence_share", silence, fmt)?.vars).toEqual({ longest_s: "3", timeout_s: "5", from_s: "20" });
    expect(metricNote("link.txbuf_low", ev("r", { reported_by: "ground" }), fmt)?.key).toBe("links.note.txbuf.ground");
    expect(metricNote("link.rssi_asymmetry", ev("a", { weaker: "aircraft" }), fmt)?.key).toBe("links.note.weaker.aircraft");
    expect(metricNote("link.rc_receiver_failsafes", ev("f", { count: 0, longest_s: 0 }), fmt)).toBeNull();
    expect(metricNote("link.fade_margin", ev("f", { aircraft_db: 20 }), fmt)).toBeNull(); // one end only
    expect(metricNote("link.downlink_loss", undefined, fmt)).toBeNull();
    expect(metricNote("link.unknown", down, fmt)).toBeNull();
  });

  it("words each setting's value and verdict by what the engine said", () => {
    const radio = row({ action: "RTL", wait_param: "RC_FS_TIMEOUT", wait_s: 1, verdict: "first" });
    expect(valueNote(radio, fmt)).toEqual({ key: "links.value.radio", vars: { value: "1", action: "RTL", s: "1", param: "RC_FS_TIMEOUT" } });
    expect(verdictNote(radio, fmt)).toEqual({ key: "links.verdict.first", vars: {} });
    const gcs = row({ param: "FS_GCS_ENABLE", action: "RTL", wait_param: "FS_GCS_TIMEOUT", wait_s: 5 });
    expect(valueNote(gcs, fmt).key).toBe("links.value.gcs");
    expect(valueNote(row({ value: 0, action: "NONE", wait_s: 1 }), fmt).key).toBe("links.value.none");
    const timeout = row({ param: "FS_GCS_TIMEOUT", value: 5, longest_s: 3, margin_s: 2, status: "caution", verdict: "margin" });
    expect(valueNote(timeout, fmt)).toEqual({ key: "links.value.timeoutLongest", vars: { value: "5", longest: "3" } });
    expect(verdictNote(timeout, fmt)).toEqual({ key: "links.verdict.margin", vars: { s: "2" } });
    expect(valueNote(row({ param: "FS_GCS_TIMEOUT", value: 5, verdict: "unmeasured" }), fmt).key).toBe("links.value.timeout");
    const options = row({ param: "FS_OPTIONS", value: 16, options: ["GCS_CONTINUE_IF_PILOT_CONTROL"], verdict: "options" });
    expect(valueNote(options, fmt).vars.options).toBe("GCS_CONTINUE_IF_PILOT_CONTROL");
    expect(valueNote(row({ param: "FS_OPTIONS", value: 0, options: [] }), fmt).key).toBe("links.value.noOptions");
    const override = row({ param: "RC_OVERRIDE_TIME", value: 5, default: 3, verdict: "changed" });
    expect(valueNote(override, fmt).key).toBe("links.value.seconds");
    expect(verdictNote(override, fmt)).toEqual({ key: "links.verdict.changed", vars: { value: "3" } });
  });

  it("has wording for every verdict and setting the engine sends", () => {
    const verdicts = ["off", "first", "on", "backstop", "off_pilot", "margin", "reached", "unmeasured", "options", "default", "changed", "value"];
    for (const v of verdicts) expect(CATALOGS.en[`links.verdict.${v}` as MessageKey], v).toBeTruthy();
    for (const param of ["FS_THR_ENABLE", "FS_GCS_ENABLE", "FS_GCS_TIMEOUT", "FS_OPTIONS", "RC_OVERRIDE_TIME"]) {
      for (const control of ["station", "receiver"] as const) {
        expect(CATALOGS.en[settingKey(row({ param }), control)], param).toBeTruthy();
      }
    }
    expect(settingKey(row({}), "station")).toBe("links.setting.FS_THR_ENABLE.station");
  });

  it("says what each kind of radio reports, and which is this aircraft's", () => {
    const none = radioRows({ downlink: null, uplink: null, radio: null, radio_kind: "Doodle Labs" });
    expect(none.map((r) => [r.id, r.reading, r.status, r.mine])).toEqual([
      ["mavlink", "none", "info", false], ["sik", "none", "info", false], ["mesh", "later", "info", true],
    ]);
    const radio = { reported_by: "aircraft" as const, sik: false, slows_streams: 50, txbuf: { t: [], lo: [], hi: [] } };
    const silent = radioRows({ downlink: null, uplink: { links: [] }, radio: null, radio_kind: null });
    expect(silent[0]!.reading).toBe("none"); // MAV logged, but no ground station heard
    const other = radioRows({ downlink: null, uplink: { links: [], unheard: { t: [], lo: [], hi: [] } }, radio, radio_kind: null });
    expect(other.map((r) => r.reading)).toEqual(["read", "txbuf", "later"]);
    const sik = radioRows({ downlink: null, uplink: null, radio: { ...radio, sik: true }, radio_kind: "RFD" });
    expect(sik[1]).toEqual({ id: "sik", reading: "read", status: "ok", mine: true });
  });

  it("finds the longest stretch, the worst loss and the slowest station", () => {
    expect(longestSpan([{ from_s: 1, to_s: 2 }, { from_s: 5, to_s: 9 }])).toBe(4);
    expect(longestSpan([])).toBeNull();
    const loss = (status: Loss["status"]): Loss => ({ id: "radio_lost", title: { en: "", uk: "" }, lines: [], alone: [], alone_names: [], status });
    expect(worstLoss([loss("ok"), loss("out")])).toBe("out");
    expect(worstLoss([])).toBe("info");
    expect(delayStation([station(1, [0.5, 3]), station(2, [0.4, 9]), station(3, null)])?.station).toBe(2);
    expect(delayStation([station(1, null)])).toBeNull();
  });
});
