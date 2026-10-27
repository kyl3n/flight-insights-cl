// The Links tab of one flight, as the engine's flight.links returns it, and the pure
// helpers behind it (spec: Links, ground station and whole-system faults, "The Links
// tab"; mock-up Links): loss, outages and silences over time in seconds after arming,
// the radios and the receiver, and the failsafe settings for the link with what losing
// it does in each mode. Every verdict is the engine's: the link analyzer's zones, and the
// settings' statuses; this file only picks and lays them out.

import type { Envelope } from "./flightviews";
import type { MessageKey } from "./i18n";
import { sortFindings, type Analysis, type EvidenceRow, type FindingRow, type MetricValue, type Text } from "./report";
import type { Status } from "./status";

export interface Span {
  from_s: number; // seconds after arming
  to_s: number;
}

export interface StationView {
  station: number;
  received: number;
  lost: number;
  out_of_order: number;
  loss_pct: number;
  longest_s: number;
  longest_at_s: number;
  outages: number; // silences over outage_s
  clock: string; // the tlog's clock fit: aligned, corrected or unconfirmed
  delay_ms: [number, number] | null; // median and 95th percentile above the least delay
  silence: Envelope; // the silence before each packet, s
  outage_list: Span[];
}

export interface UplinkLink {
  chan: number;
  received: number;
  missed: number;
  loss_pct: number;
}

export interface Uplink {
  links: UplinkLink[];
  timeout_s?: number; // FS_GCS_TIMEOUT
  unheard?: Envelope; // how long the ground station had gone unheard, s
  longest_s?: number; // while armed
  from_s?: number;
}

export interface Joystick {
  held_pct: number; // of the armed time, an RC override from the station in force
  lapses: Span[]; // stretches with none
  override_time_s: number; // RC_OVERRIDE_TIME
}

export type End = "aircraft" | "ground";

export interface RadioStatus {
  reported_by: End;
  sik: boolean;
  slows_streams: number; // TxBuf under this percentage slows every stream
  txbuf: Envelope;
  fade_db?: Partial<Record<End, Envelope>>;
  target_db?: number | null;
}

export interface Receiver {
  ignored: boolean; // RC_OPTIONS: the ground station flies it
  rssi?: Envelope; // percent
  lq?: Envelope; // percent
  failsafes?: Span[]; // the receiver's own, while armed
}

export type Verdict =
  | "off"
  | "first"
  | "on"
  | "backstop"
  | "off_pilot"
  | "margin"
  | "reached"
  | "unmeasured"
  | "options"
  | "default"
  | "changed"
  | "value";

export interface SettingRow {
  param: string;
  value: number;
  status: Status;
  verdict: Verdict;
  action?: string; // the failsafe's action, by the firmware's name
  wait_param?: string;
  wait_s?: number;
  longest_s?: number;
  margin_s?: number;
  options?: string[];
  default?: number | null;
}

export type Control = "station" | "receiver";

export interface Loss {
  id: "link_lost" | "radio_lost" | "telemetry_lost";
  title: Text;
  lines: Text[]; // worded by the engine, as the recovery card words them
  alone: string[]; // modes left with no one flying it, by the firmware's names
  alone_names: string[]; // the same, as the recovery card names them
  status: Status;
}

export type Failsafes =
  | { skipped: "copter_only" }
  | { control: Control; settings: SettingRow[]; losses: Loss[] };

export interface LinksView {
  flight_id: string;
  armed_s: number;
  window_s: [number, number];
  simulated: boolean;
  control: Control;
  radio_kind: string | null; // the platform setup's telemetry radio
  tlogs: number;
  tlogs_missing: number;
  modes: { t: number; mode: string }[];
  downlink: { outage_s: number; stations: StationView[] } | null;
  uplink: Uplink | null;
  joystick: Joystick | null;
  radio: RadioStatus | null;
  receiver: Receiver;
  failsafes: Failsafes;
}

/** The link analyzer's measures, in the order the screen shows them. */
export const LINK_METRICS = [
  "link.downlink_loss",
  "link.downlink_outage",
  "link.uplink_loss",
  "link.gcs_silence_share",
  "link.txbuf_low",
  "link.fade_margin",
  "link.rssi_asymmetry",
  "link.rc_rssi",
  "link.rc_lq",
  "link.rc_receiver_failsafes",
  "link.rc_fs_thr_margin",
] as const;

/** The flight's link measures, in screen order; any the screen does not know come last. */
export function linkMetrics(analysis: Pick<Analysis, "metrics">): MetricValue[] {
  const rank = (id: string) => {
    const i = (LINK_METRICS as readonly string[]).indexOf(id);
    return i < 0 ? LINK_METRICS.length : i;
  };
  return analysis.metrics
    .filter((m) => m.metric.startsWith("link.") && m.segment === "flight")
    .sort((a, b) => rank(a.metric) - rank(b.metric));
}

const LINK_FAILSAFES = new Set(["FAILSAFE_RADIO", "FAILSAFE_GCS"]);

/** The findings about the links: the link analyzer's, the commands and the operator's,
 * and the failsafe findings whose evidence is a radio or ground-station failsafe; most
 * urgent first. */
export function linkFindings(analysis: Pick<Analysis, "findings" | "evidence">): FindingRow[] {
  const evidence = new Map(analysis.evidence.map((e) => [e.id, e]));
  const linked = (f: FindingRow) =>
    f.evidence.some((id) => LINK_FAILSAFES.has(String(evidence.get(id)?.values.failsafe ?? "")));
  return sortFindings(
    analysis.findings.filter(
      (f) => /^(link|command|operator)\./.test(f.code) || (f.code.startsWith("failsafe.") && linked(f)),
    ),
  );
}

export interface Note {
  key: MessageKey;
  vars: Record<string, string | number>;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** The line under a measure, from the evidence it rests on; null when the evidence
 * does not hold what the line needs. */
export function metricNote(metric: string, ev: EvidenceRow | undefined, fmt: (v: number) => string): Note | null {
  const v = ev?.values ?? {};
  const all = (...names: string[]) => names.every((n) => num(v[n]) !== null);
  const vars = (...names: string[]) => Object.fromEntries(names.map((n) => [n, fmt(num(v[n]) as number)]));
  switch (metric) {
    case "link.downlink_loss":
      return all("lost", "received")
        ? { key: "links.note.downlink", vars: { lost: fmt(num(v.lost)!), total: fmt(num(v.lost)! + num(v.received)!) } }
        : null;
    case "link.downlink_outage":
      return all("outages", "longest_at_s") ? { key: "links.note.outage", vars: vars("outages", "longest_at_s") } : null;
    case "link.uplink_loss":
      return all("missed", "received", "link")
        ? { key: "links.note.uplink", vars: { missed: fmt(num(v.missed)!), total: fmt(num(v.missed)! + num(v.received)!), link: String(v.link) } }
        : null;
    case "link.gcs_silence_share":
      return all("longest_s", "timeout_s", "from_s") ? { key: "links.note.silence", vars: vars("longest_s", "timeout_s", "from_s") } : null;
    case "link.txbuf_low":
      return v.reported_by === "aircraft" || v.reported_by === "ground"
        ? { key: `links.note.txbuf.${v.reported_by}`, vars: {} }
        : null;
    case "link.fade_margin":
      return all("aircraft_db", "ground_db") ? { key: "links.note.fade", vars: vars("aircraft_db", "ground_db") } : null;
    case "link.rssi_asymmetry":
      return v.weaker === "aircraft" || v.weaker === "ground" ? { key: `links.note.weaker.${v.weaker}`, vars: {} } : null;
    case "link.rc_rssi":
    case "link.rc_lq":
      return all("min_pct") ? { key: "links.note.lowest", vars: vars("min_pct") } : null;
    case "link.rc_receiver_failsafes":
      return all("count", "longest_s") && num(v.count)! > 0 ? { key: "links.note.receiver", vars: vars("longest_s") } : null;
    case "link.rc_fs_thr_margin":
      return all("lowest_pwm", "fs_thr_value") ? { key: "links.note.fsthr", vars: vars("lowest_pwm", "fs_thr_value") } : null;
    default:
      return null;
  }
}

/** The worded verdict of one setting, as its badge reads. */
export function verdictNote(row: SettingRow, fmt: (v: number) => string): Note {
  switch (row.verdict) {
    case "margin":
      return { key: "links.verdict.margin", vars: { s: fmt(row.margin_s ?? 0) } };
    case "changed":
      return { key: "links.verdict.changed", vars: { value: row.default === null || row.default === undefined ? "–" : fmt(row.default) } };
    default:
      return { key: `links.verdict.${row.verdict}` as MessageKey, vars: {} };
  }
}

/** What a setting's value means: its action and wait for a failsafe, this flight's
 * longest silence for the wait, the options set, or the default. */
export function valueNote(row: SettingRow, fmt: (v: number) => string): Note {
  const value = fmt(row.value);
  if (row.action !== undefined && row.wait_s !== undefined) {
    if (row.action === "NONE") return { key: "links.value.none", vars: { value } };
    return {
      key: row.param === "FS_GCS_ENABLE" ? "links.value.gcs" : "links.value.radio",
      vars: { value, action: row.action, s: fmt(row.wait_s), param: row.wait_param ?? "" },
    };
  }
  if (row.param === "FS_GCS_TIMEOUT") {
    return row.longest_s === undefined
      ? { key: "links.value.timeout", vars: { value } }
      : { key: "links.value.timeoutLongest", vars: { value, longest: fmt(row.longest_s) } };
  }
  if (row.options !== undefined) {
    return row.options.length
      ? { key: "links.value.options", vars: { value, options: row.options.join(", ") } }
      : { key: "links.value.noOptions", vars: { value } };
  }
  return { key: "links.value.seconds", vars: { value } };
}

/** The setting's own line: what it is for this aircraft's control. */
export function settingKey(row: SettingRow, control: Control): MessageKey {
  if (row.param === "FS_THR_ENABLE") return control === "station" ? "links.setting.FS_THR_ENABLE.station" : "links.setting.FS_THR_ENABLE.receiver";
  if (row.param === "FS_GCS_ENABLE") return control === "station" ? "links.setting.FS_GCS_ENABLE.station" : "links.setting.FS_GCS_ENABLE.receiver";
  return `links.setting.${row.param}` as MessageKey;
}

export type Reading = "read" | "txbuf" | "none" | "later";

export interface RadioRow {
  id: "mavlink" | "sik" | "mesh";
  reading: Reading;
  status: Status;
  mine: boolean; // the radio the platform setup names
}

const MESH = new Set(["Doodle Labs", "Silvus", "DigiQ SkyPulse", "IP mesh, other", "wfb-ng"]);

/** What the app reads from each kind of radio, and whether this flight had it. The
 * radios' own logs (IP mesh radios and wfb-ng) are a later import. */
export function radioRows(view: Pick<LinksView, "downlink" | "uplink" | "radio" | "radio_kind">): RadioRow[] {
  const heard = view.uplink !== null && (view.uplink.links.length > 0 || view.uplink.unheard !== undefined);
  const mavlink: Reading = view.downlink || heard ? "read" : "none";
  const sik: Reading = !view.radio ? "none" : view.radio.sik ? "read" : "txbuf";
  const look = (r: Reading): Status => (r === "read" ? "ok" : r === "txbuf" ? "caution" : "info");
  const kind = view.radio_kind ?? "";
  return [
    { id: "mavlink", reading: mavlink, status: look(mavlink), mine: false },
    { id: "sik", reading: sik, status: look(sik), mine: kind === "SiK" || kind === "RFD" },
    { id: "mesh", reading: "later", status: "info", mine: MESH.has(kind) },
  ];
}

/** The worst status among the losses played: what the losses' heading shows. */
export function worstLoss(losses: Loss[]): Status {
  const order: Status[] = ["out", "caution", "ok", "info"];
  return losses.map((l) => l.status).sort((a, b) => order.indexOf(a) - order.indexOf(b))[0] ?? "info";
}

/** Each station's colour on the plots; one station is drawn in the accent's blue. */
const STATION_COLOURS = ["#4A9FD8", "#E0952B", "#2FA877", "#A86FE0"];
export const stationColour = (n: number) => STATION_COLOURS[(n - 1) % STATION_COLOURS.length]!;

/** The longest of some stretches, in seconds; null with none. */
export function longestSpan(spans: Span[]): number | null {
  return spans.length ? Math.max(...spans.map((s) => s.to_s - s.from_s)) : null;
}

/** The station whose delay the screen shows: the worst p95 of those that have one. */
export function delayStation(stations: StationView[]): StationView | null {
  const timed = stations.filter((s) => s.delay_ms !== null);
  return timed.length ? timed.reduce((a, b) => (b.delay_ms![1] > a.delay_ms![1] ? b : a)) : null;
}
