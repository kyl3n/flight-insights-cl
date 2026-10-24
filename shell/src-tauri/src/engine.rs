//! The engine sidecar: start, unlock, restart and stop (docs/SPEC.md, "System
//! architecture").
//!
//! The engine is the frozen Python program bundled next to the app
//! (`fi-engine`, as Tauri names a sidecar once installed). For development and
//! tests `FI_ENGINE_CMD` replaces it with any command, written as a JSON array,
//! such as `["uv", "run", "--project", "engine", "python", "-m", "flight_insights", "rpc"]`.
//! The engine's stderr is its log: kept as the last lines for diagnostics, never
//! mixed into the protocol.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::identity::LaptopIdentity;
use crate::rpc::{RpcClient, RpcError};

pub const CALL_TIMEOUT: Duration = Duration::from_secs(600);
const START_TIMEOUT: Duration = Duration::from_secs(60);
const STOP_TIMEOUT: Duration = Duration::from_secs(10);
const LOG_LINES: usize = 200;

#[derive(Clone, Debug)]
pub struct EngineCommand {
    pub program: PathBuf,
    pub args: Vec<String>,
}

impl EngineCommand {
    /// FI_ENGINE_CMD if set, otherwise the bundled sidecar next to the app.
    pub fn resolve(exe_dir: &Path) -> Result<Self, String> {
        Self::resolve_from(std::env::var("FI_ENGINE_CMD").ok(), exe_dir)
    }

    /// `resolve` with the variable's value given: the installers put the sidecar
    /// beside the app as `fi-engine` (tauri.bundle.conf.json, `externalBin`).
    pub fn resolve_from(command: Option<String>, exe_dir: &Path) -> Result<Self, String> {
        if let Some(text) = command {
            let parts: Vec<String> = serde_json::from_str(&text)
                .map_err(|_| "FI_ENGINE_CMD must be a JSON array of strings".to_string())?;
            let (program, args) = parts.split_first().ok_or("FI_ENGINE_CMD is empty")?;
            return Ok(EngineCommand {
                program: program.into(),
                args: args.to_vec(),
            });
        }
        let name = if cfg!(windows) {
            "fi-engine.exe"
        } else {
            "fi-engine"
        };
        Ok(EngineCommand {
            program: exe_dir.join(name),
            args: vec!["rpc".into()],
        })
    }
}

pub struct Engine {
    child: Mutex<Child>,
    pub client: RpcClient,
    log: Arc<Mutex<VecDeque<String>>>,
}

impl Engine {
    /// Start the engine and wait until it answers `engine.hello`.
    pub fn start<F>(
        cmd: &EngineCommand,
        knowledge_dir: Option<&Path>,
        on_notification: F,
    ) -> Result<(Self, Value), String>
    where
        F: Fn(&str, &Value) + Send + 'static,
    {
        let mut command = Command::new(&cmd.program);
        command
            .args(&cmd.args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(dir) = knowledge_dir {
            command.env("FI_KNOWLEDGE_DIR", dir);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        // Its own process group, so `kill` reaches the Python process that a
        // one-file bundle's bootloader starts, not only the bootloader.
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command.spawn().map_err(|e| {
            format!(
                "could not start the engine ({}): {e}",
                cmd.program.display()
            )
        })?;
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        let log: Arc<Mutex<VecDeque<String>>> = Arc::default();
        let tail = log.clone();
        thread::Builder::new()
            .name("engine-stderr".into())
            .spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let mut t = tail.lock().expect("engine log");
                    if t.len() == LOG_LINES {
                        t.pop_front();
                    }
                    t.push_back(line);
                }
            })
            .map_err(|e| e.to_string())?;
        let client = RpcClient::new(BufReader::new(stdout), stdin, on_notification);
        let engine = Engine {
            child: Mutex::new(child),
            client,
            log,
        };
        match engine.client.call("engine.hello", json!({}), START_TIMEOUT) {
            Ok(hello) => Ok((engine, hello)),
            Err(e) => {
                let detail = engine.log_tail().join("\n");
                engine.kill();
                Err(format!("the engine did not start: {e}\n{detail}"))
            }
        }
    }

    /// Open the encrypted store with this laptop's keys.
    pub fn unlock(&self, identity: &LaptopIdentity, data_dir: &Path) -> Result<Value, RpcError> {
        let dir = data_dir.to_string_lossy();
        let host = crate::station::host_name();
        let params = identity
            .unlock_params(&dir, host.as_deref())
            .map_err(RpcError::Io)?;
        self.client
            .call_raw("engine.unlock", &params, START_TIMEOUT)
    }

    pub fn is_running(&self) -> bool {
        let alive = matches!(
            self.child.lock().expect("engine process").try_wait(),
            Ok(None)
        );
        alive && !self.client.is_closed()
    }

    pub fn log_tail(&self) -> Vec<String> {
        self.log
            .lock()
            .expect("engine log")
            .iter()
            .cloned()
            .collect()
    }

    /// Ask the engine to stop, and make sure it has.
    pub fn stop(&self) {
        let _ = self.client.call("engine.shutdown", json!({}), STOP_TIMEOUT);
        let deadline = Instant::now() + STOP_TIMEOUT;
        while Instant::now() < deadline {
            if !matches!(
                self.child.lock().expect("engine process").try_wait(),
                Ok(None)
            ) {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        self.kill();
    }

    /// Kill the engine. On Unix its whole process group, while the leader is not
    /// yet reaped, so the group ID cannot belong to anything else. On Windows only
    /// the process started: a one-file bundle's own child may outlive it.
    fn kill(&self) {
        let mut child = self.child.lock().expect("engine process");
        #[cfg(unix)]
        if matches!(child.try_wait(), Ok(None))
            && let Ok(group) = libc::pid_t::try_from(child.id())
        {
            // SAFETY: killpg only sends a signal; the group is the engine's own.
            unsafe { libc::killpg(group, libc::SIGKILL) };
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        let running = matches!(self.child.get_mut().map(|c| c.try_wait()), Ok(Ok(None)));
        if running {
            self.kill();
        }
    }
}

/// Methods the UI may call through the shell. Unlocking and stopping are the
/// shell's own business, so the UI can never pass keys or stop the engine.
pub const UI_METHODS: &[&str] = &[
    "engine.hello",
    "log.summary",
    "log.analyze",
    "log.columns",
    "files.release",
    "source.register",
    "setup.schema",
    "platform.create",
    "platform.list",
    "platform.get",
    "platform.save",
    "platform.detect",
    "airframe.create",
    "airframe.list",
    "airframe.get",
    "airframe.record_change",
    "station.schema",
    "station.create",
    "station.list",
    "station.get",
    "station.rename",
    "station.save",
    "station.detect_software",
    "import.scan",
    "import.commit",
    "import.held",
    "import.resolve",
    "import.ignore",
    "station.folders",
    "station.add_folder",
    "station.remove_folder",
    "analysis.run",
    "analysis.get",
    "analysis.registry",
    "recommend.plan",
    "recommend.export",
    "recommend.set_status",
    "recommend.history",
    "trend.overview",
    "trend.series",
    "trend.events",
    "trend.platform",
    "flight.list",
    "fleet.overview",
    "flight.propulsion",
    "flight.system",
    "review.file",
    "filter.workbench",
    "ai.results",
    "ai.sent",
    "ai.spend",
];

/// The AI path's engine methods that are the shell's alone: `ai.prepare` and `ai.tool`
/// return the flight's own positions (the tripwire) for the shell's check, and the rest
/// record what the shell checked and sent.
pub const AI_SHELL_ONLY: &[&str] = &[
    "ai.prepare",
    "ai.record",
    "ai.selftest",
    "ai.exchange_spec",
    "ai.tool",
    "ai.accept",
];

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;

    /// The real engine, when FI_ENGINE_CMD names it (CI sets it); skipped otherwise.
    fn real_engine() -> Option<EngineCommand> {
        std::env::var("FI_ENGINE_CMD").ok()?;
        Some(EngineCommand::resolve(Path::new(".")).unwrap())
    }

    fn fixture() -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/sitl/4.7.0/heli-hover.bin")
    }

    #[test]
    fn the_ui_can_never_unlock_or_stop_the_engine() {
        assert!(!UI_METHODS.contains(&"engine.unlock"));
        assert!(!UI_METHODS.contains(&"engine.shutdown"));
        // this computer's facts come from the shell, never from the interface
        assert!(!UI_METHODS.contains(&"station.record_computer"));
        // which watched files are new is the watcher's question
        assert!(!UI_METHODS.contains(&"import.unknown"));
    }

    #[test]
    fn the_ui_never_reaches_the_ai_path() {
        // ai.prepare and ai.tool return the flight's own positions (the tripwire) for the
        // shell's check: the interface must never be able to ask for them. It may only
        // read what came of an exchange, which holds no tripwire.
        for m in UI_METHODS.iter().filter(|m| m.starts_with("ai.")) {
            assert!(["ai.results", "ai.sent", "ai.spend"].contains(m), "{m}");
        }
        assert!(AI_SHELL_ONLY.iter().all(|m| !UI_METHODS.contains(m)));
    }

    #[test]
    fn the_installed_engine_is_found_beside_the_app() {
        let dir = Path::new("/opt/Flight Insights");
        let cmd = EngineCommand::resolve_from(None, dir).unwrap();
        let name = if cfg!(windows) {
            "fi-engine.exe"
        } else {
            "fi-engine"
        };
        assert_eq!(cmd.program, dir.join(name));
        assert_eq!(cmd.args, ["rpc"]);
        let dev = EngineCommand::resolve_from(Some(r#"["uv","run","x"]"#.into()), dir).unwrap();
        assert_eq!(
            (dev.program, dev.args),
            (PathBuf::from("uv"), vec!["run".into(), "x".into()])
        );
        assert!(EngineCommand::resolve_from(Some("uv run".into()), dir).is_err());
        assert!(EngineCommand::resolve_from(Some("[]".into()), dir).is_err());
    }

    #[test]
    fn the_bundle_ships_the_engine_and_the_knowledge_pack() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        let text = std::fs::read_to_string(root.join("tauri.bundle.conf.json")).unwrap();
        let conf: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(conf["bundle"]["externalBin"], json!(["binaries/fi-engine"]));
        let resources = conf["bundle"]["resources"].as_object().unwrap();
        for (from, to) in resources {
            assert!(root.join(from).exists(), "{from} is missing");
            assert!(to.as_str().unwrap().starts_with("knowledge/"));
        }
        // the engine reads the pack's manifest first; the shell passes resource_dir()/knowledge
        assert!(resources.values().any(|t| t == "knowledge/manifest.json"));
        let pack = root.join("../../knowledge");
        for entry in std::fs::read_dir(&pack).unwrap().flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if entry.path().is_dir() && name != "tools" {
                let from = format!("../../knowledge/{name}/");
                assert!(
                    resources.contains_key(&from),
                    "the pack's {name}/ is not bundled"
                );
            }
        }
    }

    #[test]
    fn a_missing_engine_is_reported() {
        let cmd = EngineCommand {
            program: "/nonexistent/fi-engine".into(),
            args: vec![],
        };
        let err = Engine::start(&cmd, None, |_, _| {}).err().unwrap();
        assert!(err.contains("could not start the engine"));
    }

    /// Whether process `pid` has gone (or is only waiting to be reaped).
    #[cfg(target_os = "linux")]
    fn gone(pid: &str) -> bool {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(stat) => stat
                .rsplit_once(") ")
                .is_some_and(|(_, s)| s.starts_with('Z')),
            Err(_) => true,
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn killing_the_engine_also_kills_what_it_started() {
        // A stand-in for a one-file bundle: answers hello, then waits on a child.
        let script = r#"read line; echo '{"jsonrpc":"2.0","id":1,"result":{}}'; sleep 300 & echo "child $!" >&2; wait"#;
        let cmd = EngineCommand {
            program: "sh".into(),
            args: vec!["-c".into(), script.into()],
        };
        let (engine, _) = Engine::start(&cmd, None, |_, _| {}).unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let pid = loop {
            let found = engine
                .log_tail()
                .iter()
                .find_map(|l| l.strip_prefix("child ").map(str::to_owned));
            if let Some(pid) = found {
                break pid;
            }
            assert!(
                Instant::now() < deadline,
                "the stand-in never started its child"
            );
            thread::sleep(Duration::from_millis(10));
        };
        assert!(!gone(&pid));
        drop(engine);
        let deadline = Instant::now() + Duration::from_secs(10);
        while !gone(&pid) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(gone(&pid), "the engine's child outlived it");
    }

    #[test]
    fn the_real_engine_starts_unlocks_analyzes_and_stops() {
        let Some(cmd) = real_engine() else {
            eprintln!("FI_ENGINE_CMD not set: skipped");
            return;
        };
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let progress = Arc::new(Mutex::new(0usize));
        let p = progress.clone();
        let (engine, hello) = Engine::start(&cmd, knowledge.as_deref(), move |method, _| {
            if method == "$/progress" {
                *p.lock().unwrap() += 1;
            }
        })
        .unwrap();
        assert_eq!(hello["protocol"], 1);

        let data = std::env::temp_dir().join(format!("fi-shell-test-{}", std::process::id()));
        let (identity, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        let unlocked = engine.unlock(&identity, &data).unwrap();
        assert_eq!(unlocked["laptop"], identity.short_code());

        let path = fixture().to_string_lossy().into_owned();
        let analysis = engine
            .client
            .call("log.analyze", json!({ "path": path }), CALL_TIMEOUT)
            .unwrap();
        assert_eq!(analysis["parameters"]["vehicle"], "copter");
        assert!(*progress.lock().unwrap() >= 2);
        let first = engine
            .client
            .call(
                "source.register",
                json!({ "path": path, "kind": "dataflash" }),
                CALL_TIMEOUT,
            )
            .unwrap();
        let again = engine
            .client
            .call(
                "source.register",
                json!({ "path": path, "kind": "dataflash" }),
                CALL_TIMEOUT,
            )
            .unwrap();
        assert_eq!(first["duplicate"], false);
        assert_eq!(again["duplicate"], true);
        let station = engine
            .client
            .call(
                "station.create",
                json!({ "name": "Field laptop" }),
                CALL_TIMEOUT,
            )
            .unwrap();
        let detected = engine
            .client
            .call(
                "station.record_computer",
                json!({
                    "id": station["id"],
                    "version": station["version"],
                    "facts": crate::station::computer_facts(&data),
                }),
                CALL_TIMEOUT,
            )
            .unwrap();
        assert_eq!(detected["this_computer"], true);
        assert!(
            detected["current"]["profile"]["computer"]["memory_gb"]["value"].as_f64() > Some(0.0)
        );
        // the engine's log never holds the keys
        let log = engine.log_tail().join("\n");
        assert!(!log.contains(&hex::encode(*identity.store_keys.database)));
        engine.stop();
        let _ = std::fs::remove_dir_all(data);
    }

    #[test]
    fn the_shells_check_agrees_with_the_engine_on_a_real_packet() {
        let Some(cmd) = real_engine() else {
            eprintln!("FI_ENGINE_CMD not set: skipped");
            return;
        };
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
        let data = std::env::temp_dir().join(format!("fi-shell-ai-{}", std::process::id()));
        let (identity, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        engine.unlock(&identity, &data).unwrap();
        let call = |method: &str, params: Value| engine.client.call(method, params, CALL_TIMEOUT);

        // Both halves of the start-up self-test pass, on the same cases.
        let (gate, failed) =
            crate::scrub::gate(call("ai.selftest", json!({})).map_err(|e| e.to_string()));
        assert_eq!((gate, failed), (crate::scrub::AiGate::Ready, vec![]));

        let platform = call(
            "platform.create",
            json!({ "name": "Heli", "vehicle": "copter" }),
        )
        .unwrap();
        let airframe = call(
            "airframe.create",
            json!({ "platform_id": platform["id"], "name": "Heli 1" }),
        )
        .unwrap();
        let path = fixture().to_string_lossy().into_owned();
        let scanned = call("import.scan", json!({ "paths": [path] })).unwrap();
        let committed = call(
            "import.commit",
            json!({
                "batch": scanned["batch"],
                "decisions": [{ "log": "f1", "airframe_id": airframe["id"] }],
            }),
        )
        .unwrap();
        let flight = committed[0]["flights"][0].clone();
        let prepared = call("ai.prepare", json!({ "flight_id": flight })).unwrap();
        assert_eq!(prepared["engine"]["ok"], true, "{}", prepared["engine"]);
        let text = prepared["packet_json"].as_str().unwrap();
        let points: Vec<crate::scrub::Point> =
            serde_json::from_value(prepared["tripwire"].clone()).unwrap();
        assert!(!points.is_empty());
        assert_eq!(crate::scrub::check_packet(text, &points), vec![]);

        // The same packet with this flight's latitude planted in it is refused by the
        // shell alone, by path and rule, never by value.
        let mut planted: Value = serde_json::from_str(text).unwrap();
        planted["metrics"][0]["value"] = json!((points[0].0 * 1e4).round() / 1e4);
        let refused = crate::scrub::check_packet(&planted.to_string(), &points);
        assert!(!refused.is_empty());
        assert!(
            refused
                .iter()
                .all(|v| !v.rule.contains(&format!("{:.3}", points[0].0)))
        );

        let kept = call(
            "ai.record",
            json!({ "packet_id": prepared["packet_id"], "shell_reasons": [], "sent": true }),
        )
        .unwrap();
        assert_eq!(kept["shell_verdict"], "pass");

        // Every tool result is a packet the shell checks the same way.
        for (name, input) in [
            ("spectrum", json!({ "axis": "x", "stage": "before" })),
            ("parameter_doc", json!({ "name": "INS_HNTCH_MODE" })),
            (
                "segment_stats",
                json!({ "message": "BAT", "field": "Volt", "start_s": null, "end_s": null }),
            ),
        ] {
            let got = call(
                "ai.tool",
                json!({ "packet_id": prepared["packet_id"], "name": name, "input": input }),
            )
            .unwrap();
            assert_eq!(got["engine"]["ok"], true, "{name}: {}", got["engine"]);
            let points: Vec<crate::scrub::Point> =
                serde_json::from_value(got["tripwire"].clone()).unwrap();
            let text = got["packet_json"].as_str().unwrap();
            assert_eq!(crate::scrub::check_packet(text, &points), vec![], "{name}");
        }
        // The exchange's static parts are text the shell sends too.
        let spec = call("ai.exchange_spec", json!({})).unwrap();
        assert!(spec["system"].as_str().unwrap().len() > 200);
        assert_eq!(spec["tools"].as_array().unwrap().len(), 5);
        engine.stop();
        let _ = std::fs::remove_dir_all(data);
    }
}
