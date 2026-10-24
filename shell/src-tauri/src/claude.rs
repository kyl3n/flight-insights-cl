//! The Claude API client (docs/SPEC.md, "AI layer"): one exchange for one packet.
//!
//! The engine builds and checks the packet; this module checks it again with the
//! shell's own implementation (`scrub`), records the verdict, and only then sends it.
//! While Claude calls tools, each result is built and checked by the engine, checked
//! again here, recorded, and only then returned. The request is made of the packet,
//! the checked tool results and the exchange's static parts, which are checked too.
//! What came back goes to the engine, which checks it before anything is shown.
//!
//! Only ZDR-eligible features are used: the Messages API, prompt caching, structured
//! outputs and strict client tools. No model in the Fable or Mythos lines is ever
//! named, as primary or fallback: they require 30-day retention.

use std::collections::BTreeMap;
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use zeroize::Zeroizing;

use crate::net;
use crate::scrub::{self, Point, Violation};

pub const API: &str = "https://api.anthropic.com";
const API_VERSION: &str = "2026-10-21";
const FALLBACK_BETA: &str = "server-side-fallback-2026-06-01";
const MAX_TOKENS: u64 = 16_000;
const ATTEMPTS: u32 = 4;
const WITHHELD: &str = "The result was withheld: it did not pass the location check.";

/// A model the app calls, with its prices in US dollars per million tokens.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Model {
    pub id: &'static str,
    pub input: f64,
    pub output: f64,
    pub cache_read: f64,
    pub cache_write: f64,
    /// Retried on this model if the primary declines; never default routing.
    pub fallback: Option<&'static str>,
    pub effort: &'static str,
}

/// The analysis pass on every flight.
pub const ANALYSIS: Model = Model {
    id: "claude-opus-5-5",
    input: 4.0,
    output: 20.0,
    cache_read: 0.20,
    cache_write: 5.0,
    fallback: Some("claude-opus-5"),
    effort: "high",
};
/// Chat follow-ups. No fallback: a declined question is shown as declined.
pub const CHAT: Model = Model {
    id: "claude-sonnet-5-5",
    input: 2.0,
    output: 10.0,
    cache_read: 0.20,
    cache_write: 2.5,
    fallback: None,
    effort: "low",
};
const OPUS_FALLBACK: Model = Model {
    id: "claude-opus-5",
    input: 5.0,
    output: 25.0,
    cache_read: 0.50,
    cache_write: 6.25,
    fallback: None,
    effort: "high",
};
const MODELS: [Model; 3] = [ANALYSIS, CHAT, OPUS_FALLBACK];

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
}

impl Usage {
    fn add(&mut self, u: &Value) {
        let n = |k: &str| u[k].as_u64().unwrap_or(0);
        self.input_tokens += n("input_tokens");
        self.output_tokens += n("output_tokens");
        self.cache_read_tokens += n("cache_read_input_tokens");
        self.cache_write_tokens += n("cache_creation_input_tokens");
    }
}

/// What tokens cost on the model that served them; an unknown model is priced as the
/// dearest one, so the cap errs on the safe side.
pub fn cost(model: &str, u: &Usage) -> f64 {
    let m = MODELS
        .iter()
        .find(|m| m.id == model)
        .copied()
        .unwrap_or(Model {
            input: MODELS.iter().map(|m| m.input).fold(0.0, f64::max),
            output: MODELS.iter().map(|m| m.output).fold(0.0, f64::max),
            cache_read: MODELS.iter().map(|m| m.cache_read).fold(0.0, f64::max),
            cache_write: MODELS.iter().map(|m| m.cache_write).fold(0.0, f64::max),
            ..ANALYSIS
        });
    (u.input_tokens as f64 * m.input
        + u.output_tokens as f64 * m.output
        + u.cache_read_tokens as f64 * m.cache_read
        + u.cache_write_tokens as f64 * m.cache_write)
        / 1e6
}

/// The most a request with this body could cost on `m`, before it is sent: every
/// byte as an uncached input token (more than it is), and a full reply.
fn worst_case(m: &Model, body: &str) -> f64 {
    (body.len() as f64 * m.input + MAX_TOKENS as f64 * m.output.max(OPUS_FALLBACK.output)) / 1e6
}

/// The engine methods an exchange needs.
pub trait Engine {
    fn call(&self, method: &str, params: Value) -> Result<Value, String>;
}

#[derive(Debug, Clone, PartialEq)]
pub enum ApiError {
    /// The key is missing, wrong or revoked: nothing is retried.
    Key,
    /// The API refused the request itself; retrying would not help.
    Rejected(String),
    /// Offline, rate limited or overloaded, after the retries.
    Unavailable(String),
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApiError::Key => write!(f, "the Claude API key was not accepted"),
            ApiError::Rejected(e) => write!(f, "the Claude API refused the request: {e}"),
            ApiError::Unavailable(e) => write!(f, "the Claude API could not be reached: {e}"),
        }
    }
}

/// The Messages endpoint, with the key held only here.
pub struct Api {
    url: net::Url,
    key: Zeroizing<String>,
    http: net::Client,
    /// Waits between retries, longest last; shortened in tests.
    pub backoff: Vec<Duration>,
}

impl Api {
    pub fn new(base: &str, key: Zeroizing<String>) -> Result<Self, String> {
        let url = net::Url::parse(base)
            .and_then(|b| b.join("/v1/messages"))
            .map_err(|e| e.to_string())?;
        net::allowed(&url)?;
        Ok(Api {
            url,
            key,
            http: net::client(Duration::from_secs(600))?,
            backoff: [2, 8, 30].map(Duration::from_secs).to_vec(),
        })
    }

    /// One request, retried when the API is busy or the network drops.
    pub fn send(&self, body: &str, fallback: bool) -> Result<Value, ApiError> {
        let mut last = String::new();
        for attempt in 0..ATTEMPTS {
            let mut req = self
                .http
                .post(self.url.clone())
                .header("x-api-key", self.key.as_str())
                .header("anthropic-version", API_VERSION)
                .header("content-type", "application/json");
            if fallback {
                req = req.header("anthropic-beta", FALLBACK_BETA);
            }
            let wait = match req.body(body.to_owned()).send() {
                Ok(resp) => {
                    let status = resp.status();
                    let retry_after = resp
                        .headers()
                        .get("retry-after")
                        .and_then(|v| v.to_str().ok())
                        .and_then(|v| v.parse::<u64>().ok())
                        .map(|s| Duration::from_secs(s.min(60)));
                    let text = resp.text().unwrap_or_default();
                    if status.is_success() {
                        return serde_json::from_str(&text)
                            .map_err(|_| ApiError::Rejected("the reply was not JSON".into()));
                    }
                    let kind = serde_json::from_str::<Value>(&text)
                        .ok()
                        .and_then(|v| v["error"]["type"].as_str().map(str::to_owned))
                        .unwrap_or_else(|| status.as_str().to_owned());
                    match status.as_u16() {
                        401 | 403 => return Err(ApiError::Key),
                        429 | 500 | 502 | 503 | 504 | 529 => {
                            last = kind;
                            retry_after
                        }
                        _ => return Err(ApiError::Rejected(kind)),
                    }
                }
                Err(e) => {
                    last = if e.is_timeout() {
                        "timed out".into()
                    } else {
                        "no connection".into()
                    };
                    None
                }
            };
            if attempt + 1 < ATTEMPTS {
                let step = self.backoff[(attempt as usize).min(self.backoff.len() - 1)];
                thread::sleep(wait.unwrap_or(step));
            }
        }
        Err(ApiError::Unavailable(last))
    }
}

/// The exchange's static parts, from the engine (`ai.exchange_spec`).
#[derive(Debug, Clone, Deserialize)]
pub struct Spec {
    pub exchange: String,
    pub system: String,
    pub tools: Vec<Value>,
    pub reply_schema: Value,
    pub language: BTreeMap<String, String>,
    pub max_tool_rounds: usize,
}

impl Spec {
    /// The static parts must pass the shell's own detectors like any other text sent.
    pub fn check(&self) -> Vec<Violation> {
        scrub::check_content(&json!({
            "system": self.system, "tools": self.tools,
            "reply_schema": self.reply_schema, "language": self.language,
        }))
    }
}

/// How an exchange ended, for the UI.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum Outcome {
    /// Sent, and the engine recorded what came back (accepted, invalid, declined, failed).
    Done { exchange: Value },
    /// One of the checks refused the packet: nothing left the laptop.
    Refused { by: String },
    /// The monthly spend cap would be passed: nothing left the laptop.
    Capped,
    /// The static parts failed the shell's check: nothing is sent until that is fixed.
    Blocked,
}

/// What an exchange needs.
pub struct Job<'a> {
    pub engine: &'a dyn Engine,
    pub api: &'a Api,
    pub model: Model,
    pub language: &'a str,
    /// Spent this month before this exchange, and the cap; None: a person asked, the
    /// cap does not hold this one back.
    pub budget: Option<(f64, f64)>,
}

fn points(v: &Value) -> Vec<Point> {
    serde_json::from_value(v.clone()).unwrap_or_default()
}

fn reasons(v: &[Violation]) -> Value {
    serde_json::to_value(v).unwrap_or_default()
}

fn request(job: &Job, spec: &Spec, messages: &[Value]) -> String {
    let mut body = json!({
        "model": job.model.id,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": spec.system, "cache_control": {"type": "ephemeral"}}],
        "tools": spec.tools,
        "messages": messages,
        "output_config": {
            "format": {"type": "json_schema", "schema": spec.reply_schema},
            "effort": job.model.effort,
        },
        "cache_control": {"type": "ephemeral"},
    });
    if let Some(fb) = job.model.fallback {
        body["fallbacks"] = json!([{ "model": fb }]);
    }
    body.to_string()
}

/// One flight's analysis exchange, end to end.
pub fn analyse(job: &Job, flight_id: &str) -> Result<Outcome, String> {
    let spec: Spec = serde_json::from_value(job.engine.call("ai.exchange_spec", json!({}))?)
        .map_err(|e| format!("the exchange's static parts did not read: {e}"))?;
    if !spec.check().is_empty() {
        return Ok(Outcome::Blocked);
    }
    let prepared = job.engine.call(
        "ai.prepare",
        json!({ "flight_id": flight_id, "purpose": "analysis" }),
    )?;
    let packet_id = prepared["packet_id"].clone();
    if prepared["engine"]["ok"] != true {
        job.engine.call(
            "ai.record",
            json!({ "packet_id": packet_id, "shell_reasons": [], "sent": false }),
        )?;
        return Ok(Outcome::Refused {
            by: "engine".into(),
        });
    }
    let text = prepared["packet_json"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    let tripwire = points(&prepared["tripwire"]);
    let refused = scrub::check_packet(&text, &tripwire);
    let instruction = spec
        .language
        .get(job.language)
        .or_else(|| spec.language.get("en"))
        .cloned()
        .unwrap_or_default();
    let mut messages = vec![json!({"role": "user", "content": [
        {"type": "text", "text": instruction},
        {"type": "text", "text": text},
    ]})];
    let first = request(job, &spec, &messages);
    let capped = job
        .budget
        .is_some_and(|(spent, cap)| spent + worst_case(&job.model, &first) > cap);
    job.engine.call(
        "ai.record",
        json!({
            "packet_id": packet_id, "shell_reasons": reasons(&refused),
            "sent": refused.is_empty() && !capped,
        }),
    )?;
    if !refused.is_empty() {
        return Ok(Outcome::Refused { by: "shell".into() });
    }
    if capped {
        return Ok(Outcome::Capped);
    }

    let mut usage = Usage::default();
    let mut served = job.model.id.to_owned();
    let mut cost_so_far = 0.0;
    let mut calls = 0u32;
    let mut body = first;
    let finish = |stop: Option<&str>,
                  reply: Option<String>,
                  failure: Option<String>,
                  served: &str,
                  usage: &Usage,
                  calls: u32|
     -> Result<Outcome, String> {
        let exchange = job.engine.call(
            "ai.accept",
            json!({
                "packet_id": packet_id, "model": served, "stop_reason": stop, "reply": reply,
                "usage": usage, "tool_calls": calls, "cost_usd": cost(served, usage),
                "failure": failure,
            }),
        )?;
        Ok(Outcome::Done { exchange })
    };
    for round in 0..=spec.max_tool_rounds {
        let resp = match job.api.send(&body, job.model.fallback.is_some()) {
            Ok(r) => r,
            Err(e) => return finish(None, None, Some(e.to_string()), &served, &usage, calls),
        };
        usage.add(&resp["usage"]);
        if let Some(m) = resp["model"].as_str() {
            served = m.to_owned();
        }
        cost_so_far = cost(&served, &usage).max(cost_so_far);
        let stop = resp["stop_reason"].as_str().unwrap_or_default().to_owned();
        let content = resp["content"].as_array().cloned().unwrap_or_default();
        if stop != "tool_use" {
            let reply: String = content
                .iter()
                .filter(|b| b["type"] == "text")
                .filter_map(|b| b["text"].as_str())
                .collect();
            let reply = (stop == "end_turn").then_some(reply);
            return finish(Some(&stop), reply, None, &served, &usage, calls);
        }
        if round == spec.max_tool_rounds {
            break;
        }
        let mut results = Vec::new();
        for block in content.iter().filter(|b| b["type"] == "tool_use") {
            calls += 1;
            results.push(tool_result(job, &packet_id, block)?);
        }
        messages.push(json!({"role": "assistant", "content": content}));
        messages.push(json!({"role": "user", "content": results}));
        body = request(job, &spec, &messages);
        if let Some((spent, cap)) = job.budget
            && spent + cost_so_far + worst_case(&job.model, &body) > cap
        {
            let failure = Some("the monthly spend cap was reached".to_owned());
            return finish(None, None, failure, &served, &usage, calls);
        }
    }
    let failure = Some(format!(
        "more than {} rounds of tool calls",
        spec.max_tool_rounds
    ));
    finish(None, None, failure, &served, &usage, calls)
}

/// One tool call, run and checked by the engine, checked again here, recorded, and
/// returned as Claude will see it: the checked text, or that it was withheld.
fn tool_result(job: &Job, parent: &Value, block: &Value) -> Result<Value, String> {
    let id = block["id"].clone();
    let got = job.engine.call(
        "ai.tool",
        json!({ "packet_id": parent, "name": block["name"], "input": block["input"] }),
    );
    let withheld = |id: &Value| json!({"type": "tool_result", "tool_use_id": id, "content": WITHHELD, "is_error": true});
    let got = match got {
        Ok(g) => g,
        // A tool Claude named wrongly, or input that does not fit: say so, no data.
        Err(_) => {
            return Ok(json!({"type": "tool_result", "tool_use_id": id,
                             "content": "The tool could not run with that input.", "is_error": true}));
        }
    };
    if got["engine"]["ok"] != true {
        job.engine.call(
            "ai.record",
            json!({ "packet_id": got["packet_id"], "shell_reasons": [], "sent": false }),
        )?;
        return Ok(withheld(&id));
    }
    let text = got["packet_json"].as_str().unwrap_or_default().to_owned();
    let refused = scrub::check_packet(&text, &points(&got["tripwire"]));
    job.engine.call(
        "ai.record",
        json!({ "packet_id": got["packet_id"], "shell_reasons": reasons(&refused),
                "sent": refused.is_empty() }),
    )?;
    if !refused.is_empty() {
        return Ok(withheld(&id));
    }
    Ok(json!({"type": "tool_result", "tool_use_id": id, "content": text}))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    /// A local stand-in for the API: answers each request with the next canned reply
    /// and keeps what it was sent.
    /// A request as the fake API saw it: its headers and its body.
    pub type Seen = Arc<Mutex<Vec<(Vec<(String, String)>, String)>>>;

    pub struct FakeApi {
        pub url: String,
        pub seen: Seen,
    }

    /// A canned reply: status, extra headers, body.
    pub type Canned = (u16, Vec<(&'static str, String)>, String);

    pub fn fake_api(replies: Vec<Canned>) -> FakeApi {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen: Seen = Arc::default();
        let log = seen.clone();
        thread::spawn(move || {
            for (status, headers, body) in replies {
                let Ok((stream, _)) = listener.accept() else {
                    return;
                };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut head = Vec::new();
                let mut length = 0usize;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    let line = line.trim_end().to_owned();
                    if line.is_empty() {
                        break;
                    }
                    if let Some((k, v)) = line.split_once(": ") {
                        if k.eq_ignore_ascii_case("content-length") {
                            length = v.parse().unwrap();
                        }
                        head.push((k.to_ascii_lowercase(), v.to_owned()));
                    }
                }
                let mut buf = vec![0; length];
                reader.read_exact(&mut buf).unwrap();
                log.lock()
                    .unwrap()
                    .push((head, String::from_utf8(buf).unwrap()));
                let mut out = stream;
                let extra: String = headers
                    .iter()
                    .map(|(k, v)| format!("{k}: {v}\r\n"))
                    .collect();
                let _ = write!(
                    out,
                    "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n{extra}\r\n{body}",
                    body.len()
                );
            }
        });
        FakeApi { url, seen }
    }

    pub fn message(stop: &str, content: Value) -> String {
        json!({"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
               "content": content, "stop_reason": stop,
               "usage": {"input_tokens": 1200, "output_tokens": 300,
                         "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0}})
        .to_string()
    }

    /// An engine that answers from fixed replies and keeps every call.
    #[derive(Default)]
    pub struct FakeEngine {
        pub calls: Mutex<Vec<(String, Value)>>,
        pub tool_packet: Mutex<Option<String>>,
        pub packet: Mutex<Option<String>>,
    }

    pub const PACKET: &str = r#"{"aircraft":{"firmware":"4.7.0","vehicle":"copter"},"purpose":"analysis","schema":"fi.ai.packet/1"}"#;
    pub const TOOL: &str = r#"{"aircraft":{"firmware":"4.7.0","vehicle":"copter"},"purpose":"tool","schema":"fi.ai.packet/1","tool":{"name":"parameter_doc","ok":true,"parameter_doc":{"name":"INS_HNTCH_MODE","current":1}}}"#;

    impl Engine for FakeEngine {
        fn call(&self, method: &str, params: Value) -> Result<Value, String> {
            self.calls
                .lock()
                .unwrap()
                .push((method.to_owned(), params.clone()));
            Ok(match method {
                "ai.exchange_spec" => json!({
                    "exchange": "fi.ai.exchange/1", "system": "You explain flights.",
                    "tools": [{"name": "parameter_doc", "description": "Docs.", "strict": true,
                               "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                                                "required": ["name"], "additionalProperties": false}}],
                    "reply_schema": {"type": "object"}, "language": {"en": "Write in English."},
                    "max_tool_rounds": 3,
                }),
                "ai.prepare" => {
                    let text = self.packet.lock().unwrap().clone().unwrap_or(PACKET.into());
                    json!({"packet_id": "p1", "engine": {"ok": true, "reasons": []},
                           "packet_json": text, "tripwire": [[12.34567, 65.4321]]})
                }
                "ai.tool" => {
                    let text = self
                        .tool_packet
                        .lock()
                        .unwrap()
                        .clone()
                        .unwrap_or(TOOL.into());
                    json!({"packet_id": "t1", "engine": {"ok": true, "reasons": []},
                           "packet_json": text, "tripwire": [[12.34567, 65.4321]]})
                }
                "ai.record" => json!({"ok": true}),
                "ai.accept" => json!({"id": "x1", "outcome": "accepted", "echo": params}),
                _ => return Err(format!("unexpected {method}")),
            })
        }
    }

    impl FakeEngine {
        pub fn methods(&self) -> Vec<String> {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .map(|(m, _)| m.clone())
                .collect()
        }
        pub fn last(&self, method: &str) -> Value {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .rev()
                .find(|(m, _)| m == method)
                .unwrap()
                .1
                .clone()
        }
        pub fn all(&self, method: &str) -> Vec<Value> {
            self.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|(m, _)| m == method)
                .map(|(_, p)| p.clone())
                .collect()
        }
    }

    fn api(url: &str) -> Api {
        let mut a = Api::new(url, Zeroizing::new("sk-test-not-a-key".into())).unwrap();
        a.backoff = vec![Duration::from_millis(5)];
        a
    }

    fn job<'a>(engine: &'a FakeEngine, api: &'a Api, budget: Option<(f64, f64)>) -> Job<'a> {
        Job {
            engine,
            api,
            model: ANALYSIS,
            language: "en",
            budget,
        }
    }

    const REPLY: &str = r#"{"summary":"ok","root_causes":[],"recommendations":[],"disagreements":[],"questions":[],"tests_to_fly":[]}"#;

    #[test]
    fn a_packet_is_checked_sent_and_its_tools_answered() {
        let tool_use = json!([{"type": "thinking", "thinking": "", "signature": "s"},
                              {"type": "tool_use", "id": "tu1", "name": "parameter_doc",
                               "input": {"name": "INS_HNTCH_MODE"}}]);
        let fake = fake_api(vec![
            (200, vec![], message("tool_use", tool_use.clone())),
            (
                529,
                vec![],
                r#"{"type":"error","error":{"type":"overloaded_error"}}"#.into(),
            ),
            (
                200,
                vec![],
                message("end_turn", json!([{"type": "text", "text": REPLY}])),
            ),
        ]);
        let engine = FakeEngine::default();
        let a = api(&fake.url);
        let out = analyse(&job(&engine, &a, Some((0.0, 20.0))), "f1").unwrap();
        assert!(matches!(out, Outcome::Done { .. }));
        assert_eq!(
            engine.methods(),
            [
                "ai.exchange_spec",
                "ai.prepare",
                "ai.record",
                "ai.tool",
                "ai.record",
                "ai.accept"
            ]
        );
        // Marked sent only once both checks passed, before it went.
        assert_eq!(
            engine.all("ai.record")[0],
            json!({"packet_id": "p1", "shell_reasons": [], "sent": true})
        );
        assert_eq!(engine.all("ai.record")[1]["sent"], true);
        let accepted = engine.last("ai.accept");
        assert_eq!(accepted["reply"], REPLY);
        assert_eq!(accepted["stop_reason"], "end_turn");
        assert_eq!(accepted["tool_calls"], 1);
        assert_eq!(accepted["usage"]["input_tokens"], 2400);
        assert!(accepted["cost_usd"].as_f64().unwrap() > 0.0);
        let seen = fake.seen.lock().unwrap();
        assert_eq!(seen.len(), 3, "the overloaded request was retried");
        let (head, first) = &seen[0];
        let header = |k: &str| head.iter().find(|(h, _)| h == k).map(|(_, v)| v.clone());
        assert_eq!(header("anthropic-version").as_deref(), Some(API_VERSION));
        assert_eq!(header("anthropic-beta").as_deref(), Some(FALLBACK_BETA));
        assert_eq!(header("x-api-key").as_deref(), Some("sk-test-not-a-key"));
        let body: Value = serde_json::from_str(first).unwrap();
        assert_eq!(body["model"], "claude-opus-5-5");
        assert_eq!(body["fallbacks"], json!([{"model": "claude-opus-5"}]));
        assert!(body.get("temperature").is_none() && body.get("thinking").is_none());
        assert_eq!(body["output_config"]["format"]["type"], "json_schema");
        // The packet goes exactly as checked; the tool result exactly as checked.
        assert_eq!(body["messages"][0]["content"][1]["text"], PACKET);
        let last: Value = serde_json::from_str(&seen[2].1).unwrap();
        assert_eq!(last["messages"][1]["content"], tool_use);
        assert_eq!(last["messages"][2]["content"][0]["content"], TOOL);
        for (_, b) in seen.iter() {
            assert!(
                !b.contains("12.3456") && !b.contains("65.432"),
                "the tripwire never leaves"
            );
        }
    }

    #[test]
    fn a_packet_the_shell_refuses_never_leaves() {
        let fake = fake_api(vec![]);
        let engine = FakeEngine::default();
        let planted = PACKET.replace("\"schema\"", "\"gps\":{\"lat\":12.3457},\"schema\"");
        *engine.packet.lock().unwrap() = Some(planted);
        let a = api(&fake.url);
        let out = analyse(&job(&engine, &a, None), "f1").unwrap();
        assert_eq!(out, Outcome::Refused { by: "shell".into() });
        let rec = engine.last("ai.record");
        assert_eq!(rec["sent"], false);
        assert!(!rec["shell_reasons"].as_array().unwrap().is_empty());
        assert!(fake.seen.lock().unwrap().is_empty());
    }

    #[test]
    fn a_tool_result_the_shell_refuses_is_withheld() {
        let fake = fake_api(vec![
            (
                200,
                vec![],
                message(
                    "tool_use",
                    json!([{"type": "tool_use", "id": "tu1",
                "name": "parameter_doc", "input": {"name": "X"}}]),
                ),
            ),
            (
                200,
                vec![],
                message("end_turn", json!([{"type": "text", "text": REPLY}])),
            ),
        ]);
        let engine = FakeEngine::default();
        *engine.tool_packet.lock().unwrap() =
            Some(TOOL.replace("\"current\":1", "\"current\":12.3457"));
        let a = api(&fake.url);
        analyse(&job(&engine, &a, None), "f1").unwrap();
        let rec = &engine.all("ai.record")[1];
        assert_eq!(rec["sent"], false);
        let seen = fake.seen.lock().unwrap();
        let last: Value = serde_json::from_str(&seen[1].1).unwrap();
        assert_eq!(last["messages"][2]["content"][0]["content"], WITHHELD);
        assert!(!seen[1].1.contains("12.3457"));
    }

    #[test]
    fn the_cap_holds_back_what_would_pass_it() {
        let fake = fake_api(vec![]);
        let engine = FakeEngine::default();
        let a = api(&fake.url);
        let out = analyse(&job(&engine, &a, Some((19.9, 20.0))), "f1").unwrap();
        assert_eq!(out, Outcome::Capped);
        assert_eq!(engine.last("ai.record")["sent"], false);
        assert!(fake.seen.lock().unwrap().is_empty());
    }

    #[test]
    fn declined_cut_off_and_failed_are_recorded_as_such() {
        for (status, body, stop, failure) in [
            (200, message("refusal", json!([])), Some("refusal"), false),
            (
                200,
                message("max_tokens", json!([{"type": "text", "text": "{\"summ"}])),
                Some("max_tokens"),
                false,
            ),
            (
                401,
                r#"{"error":{"type":"authentication_error"}}"#.to_owned(),
                None,
                true,
            ),
            (
                400,
                r#"{"error":{"type":"invalid_request_error"}}"#.to_owned(),
                None,
                true,
            ),
        ] {
            let fake = fake_api(vec![(status, vec![], body)]);
            let engine = FakeEngine::default();
            let a = api(&fake.url);
            analyse(&job(&engine, &a, None), "f1").unwrap();
            let accepted = engine.last("ai.accept");
            assert_eq!(accepted["stop_reason"].as_str(), stop);
            assert_eq!(accepted["reply"], Value::Null);
            assert_eq!(!accepted["failure"].is_null(), failure);
            assert_eq!(
                fake.seen.lock().unwrap().len(),
                1,
                "{status} is not retried"
            );
        }
    }

    #[test]
    fn busy_retries_honour_retry_after_then_give_up() {
        let busy = || {
            (
                429,
                vec![("retry-after", "0".to_owned())],
                r#"{"error":{"type":"rate_limit_error"}}"#.to_owned(),
            )
        };
        let fake = fake_api(vec![busy(), busy(), busy(), busy()]);
        let engine = FakeEngine::default();
        let a = api(&fake.url);
        analyse(&job(&engine, &a, None), "f1").unwrap();
        assert_eq!(fake.seen.lock().unwrap().len(), ATTEMPTS as usize);
        let failure = engine.last("ai.accept")["failure"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(failure.contains("rate_limit_error") && !failure.contains("sk-test"));
    }

    #[test]
    fn costs_use_the_serving_models_prices() {
        let u = Usage {
            input_tokens: 1_000_000,
            output_tokens: 100_000,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
        };
        assert!((cost("claude-opus-5-5", &u) - 6.0).abs() < 1e-9);
        assert!((cost("claude-opus-5", &u) - 7.5).abs() < 1e-9);
        assert!(cost("something-else", &u) >= cost("claude-opus-5", &u));
        for m in MODELS {
            assert!(!m.id.contains("fable") && !m.id.contains("mythos"));
            assert!(
                m.fallback
                    .is_none_or(|f| !f.contains("fable") && !f.contains("mythos"))
            );
        }
    }

    #[test]
    fn the_static_parts_are_checked_too() {
        let mut spec: Spec = serde_json::from_value(
            FakeEngine::default()
                .call("ai.exchange_spec", json!({}))
                .unwrap(),
        )
        .unwrap();
        assert!(spec.check().is_empty());
        spec.system = "The field is at lat 47.1234, lon 8.5678.".into();
        assert!(!spec.check().is_empty());
    }
}
