//! The shell's own location check, run just before an AI packet leaves the laptop
//! (docs/SPEC.md, "AI layer" → "Location scrubber": "Checked twice").
//!
//! Written apart from the engine's scrubber and sharing no code with it, so one bug
//! cannot let coordinates through. Three layers, each of which can refuse:
//!
//! 1. **Shape.** The packet must parse as this file's own types, which refuse any key
//!    they do not name, and then fit their limits: text lengths and forms, list sizes,
//!    key forms.
//! 2. **Detectors.** No text may hold a coordinate in any common notation, no number
//!    may look like a coordinate scaled by 1e7 or carry more decimals than a packet
//!    needs in the range of degrees, and no parameter may be one that reveals the place.
//! 3. **Tripwire.** No number, in the packet or inside its text, may match one of this
//!    flight's own positions: one value within about 5 m (in degrees, ×1e7, ×1e5 or
//!    radians), or a latitude and a longitude of one point both within 0.01°.
//!
//! A refusal says where and which rule, never the value.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const SCHEMA_ID: &str = "fi.ai.packet/1";

/// A latitude and longitude in degrees.
pub type Point = (f64, f64);

const FINE: f64 = 5e-5; // degrees, about 5 m
const PAIR: f64 = 0.01; // degrees
const MAX_DECIMALS: usize = 4;
const SCALED: f64 = 1e7;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Violation {
    pub path: String,
    pub rule: String,
}

fn violation(path: impl Into<String>, rule: impl Into<String>) -> Violation {
    Violation {
        path: path.into(),
        rule: rule.into(),
    }
}

// --- layer 1: the packet's shape, as this file knows it ---------------------------------

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Packet {
    schema: String,
    #[allow(dead_code)]
    purpose: Purpose,
    aircraft: Aircraft,
    answers: Option<BTreeMap<String, Atom>>,
    flight: Option<Flight>,
    #[allow(dead_code)]
    weather: Option<Weather>,
    metrics: Option<Vec<Metric>>,
    findings: Option<Vec<Finding>>,
    evidence: Option<Vec<Evidence>>,
    parameters: Option<BTreeMap<String, f64>>,
    tool: Option<Tool>,
    question: Option<String>,
    conversation: Option<Vec<Turn>>,
}

/// A chat packet's earlier turns, as typed and as answered.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Turn {
    role: Role,
    text: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Role {
    User,
    Assistant,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Purpose {
    Analysis,
    Chat,
    Explain,
    Tool,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Vehicle {
    Copter,
    Plane,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum VehicleClass {
    Multirotor,
    Helicopter,
    Plane,
    Quadplane,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Aircraft {
    vehicle: Vehicle,
    firmware: String,
    vehicle_class: Option<VehicleClass>,
    frame_class: Option<String>,
    frame_type: Option<String>,
    board_family: Option<String>,
    motor_protocol: Option<String>,
    bidirectional_dshot: Option<bool>,
    notch_mode: Option<String>,
    imu_count: Option<i64>,
    cells: Option<i64>,
    simulated: Option<bool>,
}

#[derive(Deserialize)]
#[serde(untagged)]
#[allow(dead_code)]
enum Atom {
    Bool(bool),
    Number(f64),
    Text(String),
}

#[derive(Deserialize)]
#[serde(untagged)]
enum Keyed {
    List(Vec<Option<Atom>>),
    One(Option<Atom>),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Flight {
    duration_s: Option<f64>,
    hover_s: Option<f64>,
    modes: Option<Vec<Mode>>,
}

/// The flight's modelled weather: wind, gusts, precipitation, the boundary layer and
/// the K-index. Temperature, humidity, pressure, density altitude and cloud base narrow
/// down the place, so they have no field here and a packet holding one is refused.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Weather {
    quality: Quality,
    coverage: f64,
    wind_ms: Option<f64>,
    wind_max_ms: Option<f64>,
    surface_wind_ms: Option<f64>,
    gust_max_ms: Option<f64>,
    precipitation_mm_h: Option<f64>,
    boundary_layer_m: Option<f64>,
    kp_max: Option<f64>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Quality {
    Forecast,
    Reanalysis,
    Mixed,
    None,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Mode {
    mode: String,
    s: f64,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Zone {
    Target,
    Caution,
    Out,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Metric {
    metric: String,
    segment: String,
    value: f64,
    zone: Option<Zone>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Severity {
    Info,
    Caution,
    Out,
    Safety,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Confidence {
    Low,
    Medium,
    High,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Finding {
    #[serde(rename = "ref")]
    reference: String,
    code: String,
    severity: Severity,
    confidence: Confidence,
    params: Option<BTreeMap<String, Keyed>>,
    evidence: Option<Vec<String>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum EvidenceKind {
    Metric,
    Series,
    Parameter,
    Fact,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Segment {
    kind: String,
    start_s: f64,
    end_s: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Evidence {
    #[serde(rename = "ref")]
    reference: String,
    kind: EvidenceKind,
    summary: String,
    messages: Option<Vec<String>>,
    fields: Option<Vec<String>>,
    segment: Option<Segment>,
    values: Option<BTreeMap<String, Keyed>>,
}

// What a local tool hands back: a packet of its own, purpose "tool".

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum ToolName {
    Spectrum,
    FilterSimulation,
    ParameterDoc,
    MetricHistory,
    SegmentStats,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Tool {
    name: ToolName,
    ok: bool,
    error: Option<String>,
    spectrum: Option<Spectrum>,
    filter_simulation: Option<Simulation>,
    parameter_doc: Option<ParameterDoc>,
    metric_history: Option<History>,
    segment_stats: Option<Stats>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Axis {
    X,
    Y,
    Z,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum Stage {
    Before,
    Flown,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Spectrum {
    axis: Axis,
    stage: Stage,
    segment: Option<String>,
    seconds: Option<f64>,
    rate_hz: Option<f64>,
    hz: Vec<f64>,
    db: Vec<f64>,
    peaks: Option<Vec<Peak>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Peak {
    hz: f64,
    db: f64,
    prominence_db: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Simulation {
    params: BTreeMap<String, f64>,
    current: Simulated,
    candidate: Simulated,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Simulated {
    available: bool,
    reason: Option<String>,
    noise_db: Option<f64>,
    lag_deg: Option<f64>,
    notches: Option<i64>,
    within_budget: Option<bool>,
    centres_hz: Option<Vec<f64>>,
    bandwidth_hz: Option<f64>,
    lowpass_hz: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct ParameterDoc {
    name: String,
    display: Option<String>,
    description: Option<String>,
    units: Option<String>,
    range: Option<Vec<f64>>,
    increment: Option<f64>,
    values: Option<Vec<Labelled>>,
    bits: Option<Vec<Bit>>,
    reboot: Option<bool>,
    current: Option<f64>,
    default: Option<f64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Labelled {
    value: f64,
    label: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Bit {
    bit: i64,
    label: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct History {
    metric: String,
    segment: String,
    unit: Option<String>,
    points: Vec<HistoryPoint>,
    baseline: Option<Band>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct HistoryPoint {
    flight: i64,
    value: f64,
    zone: Option<Zone>,
    good: Option<bool>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Band {
    low: f64,
    high: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct Stats {
    message: String,
    field: String,
    start_s: f64,
    end_s: f64,
    unit: Option<String>,
    seconds: Option<f64>,
    rate_hz: Option<f64>,
    min: Option<f64>,
    p05: Option<f64>,
    p50: Option<f64>,
    mean: Option<f64>,
    p95: Option<f64>,
    max: Option<f64>,
}

static FIRMWARE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+\.\d+\.\d+$").unwrap());
static NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z][a-z0-9_.]{0,63}$").unwrap());
static KEY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z][a-z0-9_]{0,40}$").unwrap());
static REFERENCE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[EF][0-9]{1,4}$").unwrap());
static MODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Z0-9_]{1,24}$").unwrap());
static MESSAGE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Z0-9]{2,4}$").unwrap());
static FIELD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_]{1,16}$").unwrap());
static PARAMETER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z][A-Z0-9_]{0,16}$").unwrap());

/// Distances the engine works out from positions on the laptop. In evidence each may only
/// be one whole number of metres: a finer value or a series could trace the path. The
/// engine's rules list them too (knowledge/rules/scrubber.json, "position_derived").
const POSITION_METRES: [&str; 6] = [
    "drift_m",
    "drift_m_per_min",
    "largest_altitude_gap_m",
    "largest_receivers_gap_m",
    "largest_reset_m",
    "largest_track_gap_m",
];

struct Limits(Vec<Violation>);

impl Limits {
    fn text(&mut self, path: &str, value: &str, max: usize) {
        if value.chars().count() > max {
            self.0.push(violation(path, "shape: text too long"));
        }
    }
    fn form(&mut self, path: &str, value: &str, form: &Regex) {
        if !form.is_match(value) {
            self.0
                .push(violation(path, "shape: text not in the allowed form"));
        }
    }
    fn count(&mut self, path: &str, n: usize, max: usize) {
        if n > max {
            self.0.push(violation(path, "shape: too many items"));
        }
    }
    fn whole_metres(&mut self, path: &str, map: &BTreeMap<String, Keyed>) {
        for key in POSITION_METRES {
            let whole = match map.get(key) {
                None => true,
                Some(Keyed::One(Some(Atom::Number(x)))) => x.is_finite() && x.fract() == 0.0,
                Some(_) => false,
            };
            if !whole {
                self.0.push(violation(
                    path,
                    "a distance worked out from positions that is not one value in whole metres",
                ));
            }
        }
    }
    fn keyed(&mut self, path: &str, map: &BTreeMap<String, Keyed>) {
        self.count(path, map.len(), 64);
        for (k, v) in map {
            if !KEY.is_match(k) {
                self.0
                    .push(violation(path, "shape: a key the packet may not hold"));
            }
            let mut atom = |a: &Option<Atom>| {
                if let Some(Atom::Text(t)) = a {
                    self.text(path, t, 200);
                }
            };
            match v {
                Keyed::One(a) => atom(a),
                Keyed::List(items) => {
                    items.iter().for_each(&mut atom);
                    self.count(path, items.len(), 64);
                }
            }
        }
    }
}

fn shape(text: &str) -> Vec<Violation> {
    let p: Packet = match serde_json::from_str(text) {
        Ok(p) => p,
        // serde's message can quote the value: say only that it does not fit.
        Err(_) => return vec![violation("$", "shape: does not fit the packet")],
    };
    let mut l = Limits(Vec::new());
    if p.schema != SCHEMA_ID {
        l.0.push(violation("$.schema", "shape: not this packet schema"));
    }
    let a = &p.aircraft;
    l.form("$.aircraft.firmware", &a.firmware, &FIRMWARE);
    for (name, value, max) in [
        ("frame_class", &a.frame_class, 40),
        ("frame_type", &a.frame_type, 40),
        ("board_family", &a.board_family, 16),
        ("motor_protocol", &a.motor_protocol, 40),
        ("notch_mode", &a.notch_mode, 40),
    ] {
        if let Some(v) = value {
            l.text(&format!("$.aircraft.{name}"), v, max);
        }
    }
    if let Some(answers) = &p.answers {
        l.count("$.answers", answers.len(), 64);
        for (k, v) in answers {
            if !KEY.is_match(k) {
                l.0.push(violation(
                    "$.answers",
                    "shape: a key the packet may not hold",
                ));
            }
            if let Atom::Text(t) = v {
                l.text("$.answers", t, 40);
            }
        }
    }
    if let Some(modes) = p.flight.as_ref().and_then(|f| f.modes.as_ref()) {
        l.count("$.flight.modes", modes.len(), 32);
        for m in modes {
            l.form("$.flight.modes", &m.mode, &MODE);
        }
    }
    if let Some(metrics) = &p.metrics {
        l.count("$.metrics", metrics.len(), 512);
        for (i, m) in metrics.iter().enumerate() {
            l.form(&format!("$.metrics[{i}].metric"), &m.metric, &NAME);
            l.form(&format!("$.metrics[{i}].segment"), &m.segment, &NAME);
        }
    }
    if let Some(findings) = &p.findings {
        l.count("$.findings", findings.len(), 256);
        for (i, f) in findings.iter().enumerate() {
            let path = format!("$.findings[{i}]");
            l.form(&path, &f.reference, &REFERENCE);
            l.form(&path, &f.code, &NAME);
            if let Some(params) = &f.params {
                l.keyed(&format!("{path}.params"), params);
            }
            if let Some(refs) = &f.evidence {
                l.count(&path, refs.len(), 32);
                refs.iter().for_each(|r| l.form(&path, r, &REFERENCE));
            }
        }
    }
    if let Some(evidence) = &p.evidence {
        l.count("$.evidence", evidence.len(), 512);
        for (i, e) in evidence.iter().enumerate() {
            let path = format!("$.evidence[{i}]");
            l.form(&path, &e.reference, &REFERENCE);
            l.text(&format!("{path}.summary"), &e.summary, 200);
            if let Some(messages) = &e.messages {
                l.count(&path, messages.len(), 16);
                messages.iter().for_each(|m| l.form(&path, m, &MESSAGE));
            }
            if let Some(fields) = &e.fields {
                l.count(&path, fields.len(), 32);
                fields.iter().for_each(|f| l.form(&path, f, &FIELD));
            }
            if let Some(s) = &e.segment {
                l.form(&path, &s.kind, &NAME);
            }
            if let Some(values) = &e.values {
                l.keyed(&format!("{path}.values"), values);
                l.whole_metres(&format!("{path}.values"), values);
            }
        }
    }
    if let Some(parameters) = &p.parameters {
        l.count("$.parameters", parameters.len(), 2000);
        for name in parameters.keys() {
            l.form("$.parameters", name, &PARAMETER);
            if removed_parameter(name) {
                l.0.push(violation("$.parameters", "a location-revealing parameter"));
            }
        }
    }
    if let Some(t) = &p.tool {
        tool(&mut l, t);
    }
    if let Some(q) = &p.question {
        l.text("$.question", q, 500);
    }
    if let Some(turns) = &p.conversation {
        l.count("$.conversation", turns.len(), 20);
        for (i, turn) in turns.iter().enumerate() {
            l.text(&format!("$.conversation[{i}].text"), &turn.text, 2000);
        }
    }
    l.0
}

fn tool(l: &mut Limits, t: &Tool) {
    let texts = |l: &mut Limits, path: &str, v: &Option<String>, max: usize| {
        if let Some(v) = v {
            l.text(path, v, max);
        }
    };
    texts(l, "$.tool.error", &t.error, 200);
    if let Some(s) = &t.spectrum {
        l.count("$.tool.spectrum.hz", s.hz.len(), 256);
        l.count("$.tool.spectrum.db", s.db.len(), 256);
        if let Some(seg) = &s.segment {
            l.form("$.tool.spectrum.segment", seg, &NAME);
        }
        if let Some(peaks) = &s.peaks {
            l.count("$.tool.spectrum.peaks", peaks.len(), 16);
        }
    }
    if let Some(s) = &t.filter_simulation {
        let path = "$.tool.filter_simulation.params";
        l.count(path, s.params.len(), 16);
        for name in s.params.keys() {
            l.form(path, name, &PARAMETER);
            if removed_parameter(name) {
                l.0.push(violation(path, "a location-revealing parameter"));
            }
        }
        for (part, sim) in [("current", &s.current), ("candidate", &s.candidate)] {
            let path = format!("$.tool.filter_simulation.{part}");
            texts(l, &path, &sim.reason, 200);
            if let Some(c) = &sim.centres_hz {
                l.count(&path, c.len(), 16);
            }
        }
    }
    if let Some(d) = &t.parameter_doc {
        let path = "$.tool.parameter_doc";
        l.form(path, &d.name, &PARAMETER);
        if removed_parameter(&d.name) {
            l.0.push(violation(path, "a location-revealing parameter"));
        }
        texts(l, path, &d.display, 200);
        texts(l, path, &d.description, 600);
        texts(l, path, &d.units, 24);
        if let Some(r) = &d.range {
            l.count(path, r.len(), 2);
        }
        if let Some(values) = &d.values {
            l.count(path, values.len(), 64);
            values.iter().for_each(|v| l.text(path, &v.label, 200));
        }
        if let Some(bits) = &d.bits {
            l.count(path, bits.len(), 32);
            bits.iter().for_each(|b| l.text(path, &b.label, 200));
        }
    }
    if let Some(h) = &t.metric_history {
        let path = "$.tool.metric_history";
        l.form(path, &h.metric, &NAME);
        l.form(path, &h.segment, &NAME);
        texts(l, path, &h.unit, 24);
        l.count(path, h.points.len(), 50);
    }
    if let Some(s) = &t.segment_stats {
        let path = "$.tool.segment_stats";
        l.form(path, &s.message, &MESSAGE);
        l.form(path, &s.field, &FIELD);
        texts(l, path, &s.unit, 24);
    }
}

/// Parameters that reveal where the aircraft is or which aircraft it is: origins and
/// positions, magnetic declination, the barometer's ground pressure and temperature and the
/// field's elevation, fences and rally points, script slots that can hold anything,
/// identities, serial numbers and the simulator's settings.
fn removed_parameter(name: &str) -> bool {
    const PREFIXES: [&str; 6] = [
        "AHRS_ORIG",
        "FENCE_",
        "RALLY_",
        "SIM_",
        "ADSB_ICAO",
        "ADSB_OFFSET_",
    ];
    const SUFFIXES: [&str; 6] = ["_LAT", "_LNG", "_LON", "_LONG", "LATITUDE", "LONGITUDE"];
    PREFIXES.iter().any(|p| name.starts_with(p))
        || SUFFIXES.iter().any(|s| name.ends_with(s))
        || name == "COMPASS_DEC"
        || name.contains("SYSID")
        || name.ends_with("SERIAL_NUM")
        || name
            .strip_prefix("SCR_USER")
            .is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
        || matches!(name, "BCN_ALT")
        || baro_ground(name)
}

/// BARO_FIELD_ELV, BARO_GND_TEMP and BAROn_GND_PRESS (n a single digit, or none).
fn baro_ground(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("BARO") else {
        return false;
    };
    let rest = rest
        .strip_prefix(|c: char| c.is_ascii_digit())
        .unwrap_or(rest);
    matches!(rest, "_GND_PRESS" | "_GND_TEMP" | "_FIELD_ELV")
}

// --- layer 2: coordinates in text and in numbers -----------------------------------------

/// A decimal comma only where no thousands group can be meant (four or more digits).
static NUMBER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[-+]?\d+(?:\.\d+|,\d{4,})?").unwrap());
static TEXT_RULES: LazyLock<Vec<(&'static str, Regex)>> = LazyLock::new(|| {
    let decimal3 = r"(?:\.\d{3,}|,\d{4,})";
    vec![
        (
            "degrees and minutes",
            Regex::new(r"\d{1,3}\s*[°º]\s*\d{1,2}\s*['′’]").unwrap(),
        ),
        (
            "a hemisphere letter",
            Regex::new(&format!(
                r"\b\d{{1,3}}(?:[.,]\d+)?\s*[°º]\s*[NSEW]\b|\b\d{{1,3}}{decimal3}\s*[NSEW]\b"
            ))
            .unwrap(),
        ),
        (
            "a pair of decimal degrees",
            Regex::new(&format!(
                r"[-+]?\d{{1,2}}{decimal3}\s*[,;/ ]\s*[-+]?\d{{1,3}}{decimal3}"
            ))
            .unwrap(),
        ),
        (
            "a latitude or longitude label",
            Regex::new(r"(?i)\b(?:lat|lon|lng|latitude|longitude)\b\s*[:=]?\s*[-+]?\d").unwrap(),
        ),
        (
            "UTM",
            Regex::new(r"\b\d{1,2}\s?[C-HJ-NP-X]\s+\d{6}(?:\.\d+)?\s*m?\s*E?\s+\d{7}").unwrap(),
        ),
        (
            "a plus code",
            Regex::new(r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b").unwrap(),
        ),
    ]
});
static MGRS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\b\d{1,2}[C-HJ-NP-X]\s?[A-HJ-NP-Z]{2}\s?(\d{2,5})\s?(\d{2,5})\b").unwrap()
});
static GEOHASH: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b[0-9bcdefghjkmnpqrstuvwxyz]{6,12}\b").unwrap());

fn text_rule(text: &str) -> Option<String> {
    for (name, rule) in TEXT_RULES.iter() {
        if rule.is_match(text) {
            return Some(format!("a coordinate in text ({name})"));
        }
    }
    if MGRS
        .captures_iter(text)
        .any(|c| c.get(1).map(|m| m.len()) == c.get(2).map(|m| m.len()))
    {
        return Some("a coordinate in text (MGRS)".into());
    }
    if GEOHASH.find_iter(text).any(|m| {
        let w = m.as_str();
        w.chars().any(|c| c.is_ascii_digit()) && w.chars().any(|c| c.is_ascii_alphabetic())
    }) {
        return Some("a coordinate in text (geohash)".into());
    }
    None
}

fn decimals(text: &str) -> usize {
    let text = text.replace(',', ".");
    match text.split_once('.') {
        Some((_, frac)) => frac.trim_end_matches('0').len(),
        None => 0,
    }
}

fn number_rule(x: f64, places: usize) -> Option<&'static str> {
    if !x.is_finite() {
        return None;
    }
    if x.abs() >= SCALED && x.fract() == 0.0 && x.abs() <= 1.81e9 {
        return Some("a number in the form of a scaled coordinate");
    }
    if (1.0..=180.0).contains(&x.abs()) && places > MAX_DECIMALS {
        return Some("more precision than a packet needs, in the range of degrees");
    }
    None
}

/// Every number in the packet and inside its text, with where it is and its decimals.
fn numbers(
    value: &Value,
    path: &str,
    out: &mut Vec<(String, f64, usize)>,
    texts: &mut Vec<(String, String)>,
) {
    match value {
        Value::Object(map) => {
            for (k, v) in map {
                numbers(v, &format!("{path}.{k}"), out, texts);
            }
        }
        Value::Array(items) => {
            for (i, v) in items.iter().enumerate() {
                numbers(v, &format!("{path}[{i}]"), out, texts);
            }
        }
        Value::Number(n) => {
            if let Some(x) = n.as_f64() {
                out.push((path.to_owned(), x, decimals(&format!("{x}"))));
            }
        }
        Value::String(s) => {
            texts.push((path.to_owned(), s.clone()));
            for m in NUMBER.find_iter(s) {
                if let Ok(x) = m.as_str().replace(',', ".").parse::<f64>() {
                    out.push((path.to_owned(), x, decimals(m.as_str())));
                }
            }
        }
        _ => {}
    }
}

// --- layer 3: this flight's own positions -------------------------------------------------

fn tripwire(found: &[(String, f64, usize)], points: &[Point]) -> Vec<Violation> {
    let mut order: Vec<usize> = (0..found.len()).collect();
    order.sort_by(|&a, &b| found[a].1.total_cmp(&found[b].1));
    let sorted: Vec<f64> = order.iter().map(|&i| found[i].1).collect();
    let near = |target: f64, tol: f64| -> Vec<usize> {
        let lo = sorted.partition_point(|&v| v < target - tol);
        let hi = sorted.partition_point(|&v| v <= target + tol);
        order[lo..hi].to_vec()
    };
    let mut out = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    let forms = [
        ("degrees", 1.0),
        ("x1e7", 1e7),
        ("x1e5", 1e5),
        ("radians", std::f64::consts::PI / 180.0),
    ];
    for &(lat, lon) in points {
        for c in [lat, lon] {
            for (form, scale) in forms {
                for i in near(c * scale, FINE * scale) {
                    if seen.insert(i) {
                        out.push(violation(
                            found[i].0.clone(),
                            format!("matches this flight's position ({form})"),
                        ));
                    }
                }
            }
        }
        let (a, b) = (near(lat, PAIR), near(lon, PAIR));
        if let Some((i, j)) = a
            .iter()
            .flat_map(|&i| b.iter().map(move |&j| (i, j)))
            .find(|(i, j)| i != j)
        {
            for k in [i, j] {
                if seen.insert(k) {
                    out.push(violation(
                        found[k].0.clone(),
                        "with another value, matches a point of this flight",
                    ));
                }
            }
        }
    }
    out
}

/// Every reason the packet may not leave; empty when it may. `text` is the packet
/// exactly as it will be sent.
pub fn check_packet(text: &str, points: &[Point]) -> Vec<Violation> {
    let mut out = shape(text);
    if let Ok(value) = serde_json::from_str::<Value>(text) {
        out.extend(check_content(&value));
        out.extend(check_tripwire(&value, points));
    }
    out
}

/// Layer 2 alone, for text the shell sends beside packets that has no packet shape of
/// its own: the exchange's static parts (system prompt, tool definitions, reply
/// schema), which hold nothing about any flight and are checked before every send.
pub fn check_content(value: &Value) -> Vec<Violation> {
    let (mut found, mut texts) = (Vec::new(), Vec::new());
    numbers(value, "$", &mut found, &mut texts);
    let mut out = Vec::new();
    for (path, t) in &texts {
        if let Some(rule) = text_rule(t) {
            out.push(violation(path.clone(), rule));
        }
    }
    for (path, x, places) in &found {
        if let Some(rule) = number_rule(*x, *places) {
            out.push(violation(path.clone(), rule));
        }
    }
    out
}

/// Layer 3 alone: no number, in `value` or inside its text, near this flight's positions.
pub fn check_tripwire(value: &Value, points: &[Point]) -> Vec<Violation> {
    let (mut found, mut texts) = (Vec::new(), Vec::new());
    numbers(value, "$", &mut found, &mut texts);
    tripwire(&found, points)
}

// --- the start-up self-test ----------------------------------------------------------------

/// The shared corpus (tests/scrubber), built into the app: every clean packet must
/// pass and every leaky one must be refused before AI calls are allowed.
const CLEAN: [(&str, &str); 5] = [
    (
        "analysis",
        include_str!("../../../tests/scrubber/packets/clean/analysis.json"),
    ),
    (
        "chat",
        include_str!("../../../tests/scrubber/packets/clean/chat.json"),
    ),
    (
        "minimal",
        include_str!("../../../tests/scrubber/packets/clean/minimal.json"),
    ),
    (
        "tool_parameter_doc",
        include_str!("../../../tests/scrubber/packets/clean/tool_parameter_doc.json"),
    ),
    (
        "tool_spectrum",
        include_str!("../../../tests/scrubber/packets/clean/tool_spectrum.json"),
    ),
];
const LEAKY: [(&str, &str); 31] = [
    (
        "bool_as_number",
        include_str!("../../../tests/scrubber/packets/leaky/bool_as_number.json"),
    ),
    (
        "chat_conversation_tripwire",
        include_str!("../../../tests/scrubber/packets/leaky/chat_conversation_tripwire.json"),
    ),
    (
        "chat_question_coordinates",
        include_str!("../../../tests/scrubber/packets/leaky/chat_question_coordinates.json"),
    ),
    (
        "chat_question_too_long",
        include_str!("../../../tests/scrubber/packets/leaky/chat_question_too_long.json"),
    ),
    (
        "compass_declination",
        include_str!("../../../tests/scrubber/packets/leaky/compass_declination.json"),
    ),
    (
        "field_elevation",
        include_str!("../../../tests/scrubber/packets/leaky/field_elevation.json"),
    ),
    (
        "long_text",
        include_str!("../../../tests/scrubber/packets/leaky/long_text.json"),
    ),
    (
        "position_metres_fine",
        include_str!("../../../tests/scrubber/packets/leaky/position_metres_fine.json"),
    ),
    (
        "position_metres_series",
        include_str!("../../../tests/scrubber/packets/leaky/position_metres_series.json"),
    ),
    (
        "precise_degrees",
        include_str!("../../../tests/scrubber/packets/leaky/precise_degrees.json"),
    ),
    (
        "raw_scaled_int",
        include_str!("../../../tests/scrubber/packets/leaky/raw_scaled_int.json"),
    ),
    (
        "removed_parameter",
        include_str!("../../../tests/scrubber/packets/leaky/removed_parameter.json"),
    ),
    (
        "text_decimal_pair",
        include_str!("../../../tests/scrubber/packets/leaky/text_decimal_pair.json"),
    ),
    (
        "text_dms",
        include_str!("../../../tests/scrubber/packets/leaky/text_dms.json"),
    ),
    (
        "text_geohash",
        include_str!("../../../tests/scrubber/packets/leaky/text_geohash.json"),
    ),
    (
        "text_hemisphere",
        include_str!("../../../tests/scrubber/packets/leaky/text_hemisphere.json"),
    ),
    (
        "text_label",
        include_str!("../../../tests/scrubber/packets/leaky/text_label.json"),
    ),
    (
        "text_mgrs",
        include_str!("../../../tests/scrubber/packets/leaky/text_mgrs.json"),
    ),
    (
        "text_plus_code",
        include_str!("../../../tests/scrubber/packets/leaky/text_plus_code.json"),
    ),
    (
        "text_utm",
        include_str!("../../../tests/scrubber/packets/leaky/text_utm.json"),
    ),
    (
        "tool_doc_label",
        include_str!("../../../tests/scrubber/packets/leaky/tool_doc_label.json"),
    ),
    (
        "tool_removed_parameter",
        include_str!("../../../tests/scrubber/packets/leaky/tool_removed_parameter.json"),
    ),
    (
        "tool_spectrum_tripwire",
        include_str!("../../../tests/scrubber/packets/leaky/tool_spectrum_tripwire.json"),
    ),
    (
        "tool_unknown_key",
        include_str!("../../../tests/scrubber/packets/leaky/tool_unknown_key.json"),
    ),
    (
        "tripwire_fine",
        include_str!("../../../tests/scrubber/packets/leaky/tripwire_fine.json"),
    ),
    (
        "tripwire_pair",
        include_str!("../../../tests/scrubber/packets/leaky/tripwire_pair.json"),
    ),
    (
        "tripwire_radians",
        include_str!("../../../tests/scrubber/packets/leaky/tripwire_radians.json"),
    ),
    (
        "unknown_aircraft_key",
        include_str!("../../../tests/scrubber/packets/leaky/unknown_aircraft_key.json"),
    ),
    (
        "unknown_key",
        include_str!("../../../tests/scrubber/packets/leaky/unknown_key.json"),
    ),
    (
        "weather_density_altitude",
        include_str!("../../../tests/scrubber/packets/leaky/weather_density_altitude.json"),
    ),
    (
        "weather_tripwire",
        include_str!("../../../tests/scrubber/packets/leaky/weather_tripwire.json"),
    ),
];
const TRIPWIRE: &str = include_str!("../../../tests/scrubber/tripwire.json");

/// A corpus file's packet, as text the check reads.
pub fn corpus_packet(file: &str) -> Result<String, String> {
    let v: Value = serde_json::from_str(file).map_err(|e| e.to_string())?;
    serde_json::to_string(&v["packet"]).map_err(|e| e.to_string())
}

pub fn corpus_points(file: &str) -> Result<Vec<Point>, String> {
    let v: Value = serde_json::from_str(file).map_err(|e| e.to_string())?;
    v["points"]
        .as_array()
        .ok_or("no points")?
        .iter()
        .map(|p| match (p[0].as_f64(), p[1].as_f64()) {
            (Some(a), Some(b)) => Ok((a, b)),
            _ => Err("a point that is not two numbers".to_string()),
        })
        .collect()
}

/// The shell's half of the start-up self-test: the names of the cases it got wrong.
pub fn self_test() -> Vec<String> {
    self_test_with(check_packet)
}

fn self_test_with(check: impl Fn(&str, &[Point]) -> Vec<Violation>) -> Vec<String> {
    let points = match corpus_points(TRIPWIRE) {
        Ok(p) => p,
        Err(e) => return vec![format!("the built-in tripwire: {e}")],
    };
    let mut failed = Vec::new();
    for (name, file) in CLEAN {
        match corpus_packet(file) {
            Ok(text) if check(&text, &points).is_empty() => {}
            _ => failed.push(format!("clean packet {name} was refused")),
        }
    }
    for (name, file) in LEAKY {
        match corpus_packet(file) {
            Ok(text) if !check(&text, &points).is_empty() => {}
            _ => failed.push(format!("leaky packet {name} was let through")),
        }
    }
    failed
}

/// Whether AI calls may be made: only once both halves of the start-up self-test pass.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AiGate {
    /// The self-test has not run yet: the engine is still starting.
    #[default]
    Pending,
    Ready,
    Blocked,
}

/// The start-up self-test: the shell's own cases, and the engine's (its `ai.selftest`
/// reply). Both must pass, and the engine must have run the same number of cases, so
/// the two check the same corpus. Returns the gate and what failed, by case name.
pub fn gate(engine: Result<Value, String>) -> (AiGate, Vec<String>) {
    let mut failed: Vec<String> = self_test()
        .into_iter()
        .map(|f| format!("shell: {f}"))
        .collect();
    match engine {
        Ok(reply) => {
            for f in reply["failed"].as_array().into_iter().flatten() {
                failed.push(format!("engine: {}", f.as_str().unwrap_or("a case failed")));
            }
            let cases = CLEAN.len() + LEAKY.len();
            if reply["cases"].as_u64() != Some(cases as u64) {
                failed.push(format!("engine: did not run the {cases} shared cases"));
            }
            if reply["ok"] != true && failed.is_empty() {
                failed.push("engine: the self-test did not pass".into());
            }
        }
        Err(e) => failed.push(format!("engine: the self-test could not run ({e})")),
    }
    let state = if failed.is_empty() {
        AiGate::Ready
    } else {
        AiGate::Blocked
    };
    (state, failed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn corpus_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/scrubber")
    }

    fn files(kind: &str) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> =
            std::fs::read_dir(corpus_dir().join("packets").join(kind))
                .unwrap()
                .map(|e| e.unwrap().path())
                .map(|p| {
                    let name = p.file_stem().unwrap().to_string_lossy().into_owned();
                    (name, std::fs::read_to_string(p).unwrap())
                })
                .collect();
        out.sort();
        out
    }

    fn points() -> Vec<Point> {
        corpus_points(&std::fs::read_to_string(corpus_dir().join("tripwire.json")).unwrap())
            .unwrap()
    }

    #[test]
    fn the_shared_corpus_gets_the_same_verdicts_as_the_engine() {
        let points = points();
        for (name, file) in files("clean") {
            let text = corpus_packet(&file).unwrap();
            assert_eq!(check_packet(&text, &points), vec![], "{name}");
        }
        for (name, file) in files("leaky") {
            let text = corpus_packet(&file).unwrap();
            let refused = check_packet(&text, &points);
            assert!(!refused.is_empty(), "{name} was let through");
            for v in refused {
                let shown = format!("{} {}", v.path, v.rule);
                for (lat, lon) in &points {
                    for value in [lat, lon] {
                        assert!(!shown.contains(&format!("{value:.2}")), "{name}: {shown}");
                    }
                }
            }
        }
    }

    #[test]
    fn the_built_in_cases_are_the_whole_corpus() {
        let leaky: Vec<String> = files("leaky").into_iter().map(|(n, _)| n).collect();
        let built: Vec<String> = LEAKY.iter().map(|(n, _)| n.to_string()).collect();
        assert_eq!(leaky, built, "add the new corpus files to LEAKY");
        assert_eq!(files("clean").len(), CLEAN.len());
        assert_eq!(self_test(), Vec::<String>::new());
    }

    #[test]
    fn the_tripwire_is_what_catches_the_tripwire_packets() {
        for (name, file) in files("leaky") {
            if name.contains("tripwire") {
                let text = corpus_packet(&file).unwrap();
                assert!(!check_packet(&text, &points()).is_empty());
                assert_eq!(check_packet(&text, &[]), vec![], "{name}");
            }
        }
    }

    #[test]
    fn engine_wording_passes() {
        for text in [
            "ESC peak temperatures 52, 53, 66 °C",
            "1,950 W at 90.1 A in hover over 312.5 s",
            "93.5° of lag at 20 Hz; thrust 5 N",
            "SERIAL2 (MAVLink2) dropped up to 120 bytes/s",
            "board CubeOrangePlus, STM32H757",
        ] {
            assert_eq!(text_rule(text), None, "{text}");
        }
    }

    #[test]
    fn numbers_planted_anywhere_are_caught() {
        // A tiny xorshift: coordinates planted at random in every numeric slot of the
        // clean packet are refused, whatever slot and whatever coordinate.
        let mut seed: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut next = || {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            (seed % 1_000_000) as f64 / 1_000_000.0
        };
        let clean: Value = serde_json::from_str(&corpus_packet(CLEAN[0].1).unwrap()).unwrap();
        for _ in 0..200 {
            let lat = -80.0 + 160.0 * next();
            let lon = -170.0 + 340.0 * next();
            let mut p = clean.clone();
            let slot = (next() * 4.0) as usize;
            let target = match slot {
                0 => &mut p["metrics"][0]["value"],
                1 => &mut p["findings"][0]["params"]["motor"],
                2 => &mut p["evidence"][0]["values"]["motor3"],
                _ => &mut p["flight"]["duration_s"],
            };
            *target = serde_json::json!((lat * 1e4).round() / 1e4);
            let text = serde_json::to_string(&p).unwrap();
            assert!(
                !check_packet(&text, &[(lat, lon)]).is_empty(),
                "{lat} {lon} in slot {slot}"
            );
        }
    }

    #[test]
    fn position_metres_are_the_rules_distances() {
        // The same list as the engine's rules, kept apart from them: the distances there
        // (keys in metres), not the counts or minutes beside them.
        let rules: Value =
            serde_json::from_str(include_str!("../../../knowledge/rules/scrubber.json")).unwrap();
        let metres = Regex::new(r"_m(_|$)").unwrap();
        let mut listed: Vec<&str> = rules["position_derived"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .filter(|k| metres.is_match(k))
            .collect();
        listed.sort_unstable();
        assert_eq!(listed, POSITION_METRES.to_vec());
    }

    #[test]
    fn a_distance_from_positions_is_one_whole_number() {
        let check = |values: &str| {
            let map: BTreeMap<String, Keyed> = serde_json::from_str(values).unwrap();
            let mut l = Limits(Vec::new());
            l.whole_metres("$", &map);
            l.0.len()
        };
        assert_eq!(
            check(r#"{"largest_track_gap_m": 62, "drift_m_per_min": 3.0}"#),
            0
        );
        assert_eq!(
            check(r#"{"rate_hz": 1000.5, "minutes_to_tolerance": 3.3}"#),
            0
        );
        assert_eq!(check(r#"{"largest_track_gap_m": 61.84}"#), 1);
        assert_eq!(check(r#"{"drift_m": [3, 5, 8]}"#), 1);
        assert_eq!(check(r#"{"drift_m": "14"}"#), 1);
        assert_eq!(check(r#"{"largest_reset_m": null}"#), 1);
    }

    #[test]
    fn the_gate_opens_only_when_both_halves_pass() {
        let n = CLEAN.len() + LEAKY.len();
        let passed = serde_json::json!({ "ok": true, "cases": n, "failed": [] });
        assert_eq!(gate(Ok(passed)), (AiGate::Ready, vec![]));
        let leaky = serde_json::json!({
            "ok": false, "cases": n, "failed": ["leaky packet text_utm was let through"]
        });
        assert_eq!(
            gate(Ok(leaky)),
            (
                AiGate::Blocked,
                vec!["engine: leaky packet text_utm was let through".to_string()]
            )
        );
        let fewer = serde_json::json!({ "ok": true, "cases": 3, "failed": [] });
        assert_eq!(gate(Ok(fewer)).0, AiGate::Blocked);
        let silent = serde_json::json!({ "ok": false, "cases": n });
        assert_eq!(gate(Ok(silent)).0, AiGate::Blocked);
        assert_eq!(gate(Err("timed out".into())).0, AiGate::Blocked);
    }

    #[test]
    fn a_broken_check_fails_the_self_test() {
        // A check that lets everything through, as a bug might, and one that refuses
        // everything: the built-in cases catch both.
        let through = self_test_with(|_, _| vec![]);
        assert_eq!(through.len(), LEAKY.len());
        assert!(through.iter().all(|f| f.ends_with("was let through")));
        let refuses = self_test_with(|_, _| vec![violation("$", "shape")]);
        assert_eq!(refuses.len(), CLEAN.len());
        assert!(refuses.iter().all(|f| f.ends_with("was refused")));
    }

    #[test]
    fn removed_parameters() {
        for name in [
            "AHRS_ORIG_LAT",
            "COMPASS_DEC",
            "FENCE_RADIUS",
            "SCR_USER3",
            "SYSID_THISMAV",
            "BRD_SERIAL_NUM",
            "BCN_LONGITUDE",
            "SIM_OPOS_LAT",
            "BARO_FIELD_ELV",
            "BARO_GND_TEMP",
            "BARO1_GND_PRESS",
            "BARO3_GND_PRESS",
        ] {
            assert!(removed_parameter(name), "{name}");
        }
        for name in [
            "ATC_RAT_PIT_P",
            "ARM_FENCE",
            "SCR_USER",
            "MOT_PWM_TYPE",
            "BARO_ALT_OFFSET",
            "BARO12_GND_PRESS",
            "BARO1_DEVID",
        ] {
            assert!(!removed_parameter(name), "{name}");
        }
    }
}
