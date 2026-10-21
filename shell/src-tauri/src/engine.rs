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
        if let Ok(text) = std::env::var("FI_ENGINE_CMD") {
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

    fn kill(&self) {
        let mut child = self.child.lock().expect("engine process");
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
}
