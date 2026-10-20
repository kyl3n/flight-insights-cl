//! JSON-RPC 2.0 client for the engine: one JSON message per line over the
//! sidecar's stdin and stdout (docs/SPEC.md, "Engine interface").
//!
//! A reader thread routes each response to the call waiting for its id, and
//! hands notifications (such as `$/progress`) to a callback. Calls may run from
//! several threads at once.

use std::collections::HashMap;
use std::fmt;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{Value, json};

/// Why a call did not return a result.
#[derive(Debug, Clone, PartialEq)]
pub enum RpcError {
    /// The engine answered with a JSON-RPC error.
    Remote {
        code: i64,
        message: String,
        data: Option<Value>,
    },
    /// The engine's output closed: it stopped or crashed.
    Closed,
    Timeout,
    Io(String),
}

impl fmt::Display for RpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            RpcError::Remote { code, message, .. } => write!(f, "engine error {code}: {message}"),
            RpcError::Closed => write!(f, "the engine is not running"),
            RpcError::Timeout => write!(f, "the engine did not answer in time"),
            RpcError::Io(e) => write!(f, "talking to the engine failed: {e}"),
        }
    }
}

impl std::error::Error for RpcError {}

type Reply = Result<Value, RpcError>;
type Pending = Arc<Mutex<HashMap<u64, Sender<Reply>>>>;

pub struct RpcClient {
    writer: Mutex<Box<dyn Write + Send>>,
    pending: Pending,
    next_id: AtomicU64,
    closed: Arc<AtomicBool>,
}

impl RpcClient {
    /// Start reading `reader` on its own thread. `on_notification` receives the
    /// method and params of every notification the engine sends.
    pub fn new<R, W, F>(reader: R, writer: W, on_notification: F) -> Self
    where
        R: BufRead + Send + 'static,
        W: Write + Send + 'static,
        F: Fn(&str, &Value) + Send + 'static,
    {
        let pending: Pending = Arc::default();
        let closed = Arc::new(AtomicBool::new(false));
        let (p, c) = (pending.clone(), closed.clone());
        thread::Builder::new()
            .name("engine-rpc-reader".into())
            .spawn(move || read_loop(reader, &p, &c, on_notification))
            .expect("spawning the engine reader thread");
        RpcClient {
            writer: Mutex::new(Box::new(writer)),
            pending,
            next_id: AtomicU64::new(1),
            closed,
        }
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    /// Call `method` and wait up to `timeout` for its result.
    pub fn call(&self, method: &str, params: Value, timeout: Duration) -> Reply {
        let params = serde_json::to_string(&params).map_err(|e| RpcError::Io(e.to_string()))?;
        self.call_raw(method, &params, timeout)
    }

    /// Call with params already written as JSON text, so a caller holding secrets
    /// can build that text in memory it wipes afterwards.
    pub fn call_raw(&self, method: &str, params_json: &str, timeout: Duration) -> Reply {
        if self.is_closed() {
            return Err(RpcError::Closed);
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = mpsc::channel();
        self.pending.lock().expect("pending calls").insert(id, tx);
        let method = serde_json::to_string(method).map_err(|e| RpcError::Io(e.to_string()))?;
        let line = zeroize::Zeroizing::new(format!(
            r#"{{"jsonrpc":"2.0","id":{id},"method":{method},"params":{params_json}}}"#
        ));
        if let Err(e) = self.write_line(&line) {
            self.pending.lock().expect("pending calls").remove(&id);
            return Err(e);
        }
        match rx.recv_timeout(timeout) {
            Ok(reply) => reply,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.pending.lock().expect("pending calls").remove(&id);
                // Ask the engine to stop the work nobody is waiting for any more.
                let _ = self.notify("$/cancelRequest", json!({ "id": id }));
                Err(RpcError::Timeout)
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(RpcError::Closed),
        }
    }

    pub fn notify(&self, method: &str, params: Value) -> Result<(), RpcError> {
        let message = json!({ "jsonrpc": "2.0", "method": method, "params": params });
        self.write_line(&message.to_string())
    }

    fn write_line(&self, line: &str) -> Result<(), RpcError> {
        let mut w = self.writer.lock().expect("engine stdin");
        w.write_all(line.as_bytes())
            .and_then(|_| w.write_all(b"\n"))
            .and_then(|_| w.flush())
            .map_err(|e| RpcError::Io(e.to_string()))
    }
}

fn read_loop<R: BufRead, F: Fn(&str, &Value)>(
    reader: R,
    pending: &Pending,
    closed: &AtomicBool,
    on_notification: F,
) {
    for line in reader.lines() {
        let Ok(line) = line else { break };
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            continue; // not protocol output; the engine logs to stderr
        };
        if let Some(method) = message.get("method").and_then(Value::as_str) {
            if message.get("id").is_none() {
                on_notification(method, message.get("params").unwrap_or(&Value::Null));
            }
            continue;
        }
        let Some(id) = message.get("id").and_then(Value::as_u64) else {
            continue;
        };
        let reply = match message.get("error") {
            Some(err) => Err(RpcError::Remote {
                code: err.get("code").and_then(Value::as_i64).unwrap_or(0),
                message: err
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                data: err.get("data").cloned(),
            }),
            None => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
        };
        if let Some(tx) = pending.lock().expect("pending calls").remove(&id) {
            let _ = tx.send(reply);
        }
    }
    closed.store(true, Ordering::SeqCst);
    for (_, tx) in pending.lock().expect("pending calls").drain() {
        let _ = tx.send(Err(RpcError::Closed));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufReader, PipeReader, PipeWriter, pipe};
    use std::sync::mpsc::Receiver;

    /// A fake engine: answers on its own thread with whatever `respond` returns.
    fn fake_engine(
        respond: impl Fn(&Value) -> Vec<Value> + Send + 'static,
    ) -> (RpcClient, Receiver<(String, Value)>) {
        let (client_read, engine_write): (PipeReader, PipeWriter) = pipe().unwrap();
        let (engine_read, client_write) = pipe().unwrap();
        thread::spawn(move || {
            let mut out = engine_write;
            for line in BufReader::new(engine_read).lines() {
                let request: Value = serde_json::from_str(&line.unwrap()).unwrap();
                for reply in respond(&request) {
                    writeln!(out, "{reply}").unwrap();
                }
            }
        });
        let (tx, rx) = mpsc::channel();
        let client = RpcClient::new(BufReader::new(client_read), client_write, move |m, p| {
            tx.send((m.to_owned(), p.clone())).unwrap();
        });
        (client, rx)
    }

    const T: Duration = Duration::from_secs(5);

    #[test]
    fn results_errors_and_notifications() {
        let (client, notes) = fake_engine(|req| {
            let id = req["id"].clone();
            match req["method"].as_str() {
                Some("echo") => vec![
                    json!({"jsonrpc": "2.0", "method": "$/progress", "params": {"fraction": 0.5}}),
                    json!({"jsonrpc": "2.0", "id": id, "result": req["params"]}),
                ],
                Some("fail") => vec![json!({"jsonrpc": "2.0", "id": id,
                    "error": {"code": -32001, "message": "locked"}})],
                _ => vec![],
            }
        });
        assert_eq!(client.call("echo", json!({"a": 1}), T), Ok(json!({"a": 1})));
        let (method, params) = notes.recv_timeout(T).unwrap();
        assert_eq!(
            (method.as_str(), params),
            ("$/progress", json!({"fraction": 0.5}))
        );
        match client.call("fail", json!({}), T) {
            Err(RpcError::Remote { code, message, .. }) => {
                assert_eq!((code, message.as_str()), (-32001, "locked"))
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn replies_out_of_order_reach_the_right_caller() {
        // Each call is answered only once the next one arrives, in reverse order.
        let held: Arc<Mutex<Vec<Value>>> = Arc::default();
        let (client, _notes) = fake_engine(move |req| {
            let mut h = held.lock().unwrap();
            h.push(req.clone());
            if h.len() < 4 {
                return vec![];
            }
            h.drain(..)
                .rev()
                .map(|r| json!({"jsonrpc": "2.0", "id": r["id"], "result": r["params"]["n"]}))
                .collect()
        });
        let client = Arc::new(client);
        let handles: Vec<_> = (0..4)
            .map(|n| {
                let c = client.clone();
                thread::spawn(move || c.call("n", json!({"n": n}), T))
            })
            .collect();
        let mut got: Vec<i64> = handles
            .into_iter()
            .map(|h| h.join().unwrap().unwrap().as_i64().unwrap())
            .collect();
        got.sort();
        assert_eq!(got, vec![0, 1, 2, 3]);
    }

    #[test]
    fn a_stopped_engine_fails_waiting_and_later_calls() {
        let (client_read, engine_write) = pipe().unwrap();
        let (_engine_read, client_write) = pipe().unwrap();
        let client = RpcClient::new(BufReader::new(client_read), client_write, |_, _| {});
        drop(engine_write); // the engine exits
        let deadline = std::time::Instant::now() + T;
        while !client.is_closed() && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(client.call("x", json!({}), T), Err(RpcError::Closed));
    }

    #[test]
    fn a_silent_engine_times_out_and_is_asked_to_cancel() {
        let (client, _notes) = fake_engine(|req| {
            assert!(req.get("method").is_some());
            vec![]
        });
        let err = client.call("slow", json!({}), Duration::from_millis(50));
        assert_eq!(err, Err(RpcError::Timeout));
    }
}
