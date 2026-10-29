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

use crate::identity::{LaptopIdentity, Secret32};
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

    /// Open the encrypted store with this laptop's keys. When the laptop has just
    /// joined a fleet, its board-ID hashes are then made again with the fleet's key,
    /// which reads every log still on disk and so gets the long timeout.
    pub fn unlock(
        &self,
        identity: &LaptopIdentity,
        data_dir: &Path,
        fleet_board: Option<&Secret32>,
    ) -> Result<Value, RpcError> {
        let dir = data_dir.to_string_lossy();
        let host = crate::station::host_name();
        let params = identity
            .unlock_params(&dir, host.as_deref(), fleet_board)
            .map_err(RpcError::Io)?;
        let unlocked = self
            .client
            .call_raw("engine.unlock", &params, START_TIMEOUT)?;
        if unlocked["rekey_needed"] == json!(true) {
            self.client
                .call("board.rekey", json!({ "host": host }), CALL_TIMEOUT)?;
        }
        Ok(unlocked)
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
impl crate::claude::Engine for Engine {
    fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.client
            .call(method, params, CALL_TIMEOUT)
            .map_err(|e| e.to_string())
    }
}

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
    "airframe.recovery_card",
    "airframe.performance_model",
    "airframe.performance_points",
    "airframe.performance",
    "airframe.range_card",
    "airframe.record_change",
    "airframe.update",
    "airframe.teams",
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
    "trend.payloads",
    "trend.events",
    "trend.attribution",
    "trend.revert_test",
    "trend.credits",
    "trend.fleet",
    "trend.platform",
    "flight.list",
    "fleet.overview",
    "flight.propulsion",
    "flight.margin",
    "flight.system",
    "flight.timeline",
    "flight.tune",
    "flight.energy",
    "flight.links",
    "flight.navigation",
    "flight.ab",
    "flight.weather",
    "review.file",
    "filter.workbench",
    "ai.results",
    "ai.chat_history",
    "ai.sent",
    "ai.spend",
    "sync.status",
    "sync.conflicts",
    "sync.resolve",
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

/// Sync's engine methods that are the shell's alone: the outbox holds the paths of
/// this laptop's source files, and only objects the shell has checked are applied.
pub const SYNC_SHELL_ONLY: &[&str] = &[
    "sync.outbox",
    "sync.sent",
    "sync.apply",
    "sync.quarantine",
    "sync.unknown",
    "board.rekey",
];

/// Weather's engine methods, the shell's alone: the tiles the fleet flies in are for the
/// downloader, and the interface has no use for them or for the replies kept.
pub const WEATHER_SHELL_ONLY: &[&str] = &[
    "weather.tiles",
    "weather.store",
    "weather.held",
    "weather.export",
    "weather.refresh",
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
            assert!(
                ["ai.results", "ai.chat_history", "ai.sent", "ai.spend"].contains(m),
                "{m}"
            );
        }
        assert!(AI_SHELL_ONLY.iter().all(|m| !UI_METHODS.contains(m)));
    }

    #[test]
    fn the_ui_reads_sync_state_but_never_moves_objects() {
        for m in UI_METHODS.iter().filter(|m| m.starts_with("sync.")) {
            assert!(
                ["sync.status", "sync.conflicts", "sync.resolve"].contains(m),
                "{m}"
            );
        }
        assert!(SYNC_SHELL_ONLY.iter().all(|m| !UI_METHODS.contains(m)));
    }

    #[test]
    fn the_ui_never_reads_where_the_fleet_flies() {
        assert!(UI_METHODS.iter().all(|m| !m.starts_with("weather.")));
        assert!(WEATHER_SHELL_ONLY.iter().all(|m| !UI_METHODS.contains(m)));
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

    /// Three laptops, three real engines, one folder drive: an import on one reaches
    /// the others whole and is analysed there; an object held back leaves a gap that
    /// fills when it arrives; a killed engine picks up where it stopped.
    #[test]
    fn three_laptops_converge_through_a_folder_drive() {
        use crate::sync::drive::{Drive, FolderDrive};
        use crate::sync::keyring::{Device, Keyring, create_fleet, enroll};
        use crate::sync::run::{Calls, Syncer, publish, read_registry};

        let Some(cmd) = real_engine() else {
            eprintln!("FI_ENGINE_CMD not set: skipped");
            return;
        };
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let root = std::env::temp_dir().join(format!("fi-sync-3-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let drive = FolderDrive::new(root.join("drive")).unwrap();
        let ids: Vec<LaptopIdentity> = (0..3)
            .map(|_| {
                LaptopIdentity::load_or_create(&MemoryStore::default())
                    .unwrap()
                    .0
            })
            .collect();
        let (first, keys_a, _) = create_fleet(&ids[0], "A").unwrap();
        let reg_dir = root.join("registry");
        publish(&drive, &reg_dir, &first, 0).unwrap();
        for (n, id) in ids.iter().enumerate().skip(1) {
            let registry = read_registry(&drive, &reg_dir, None).unwrap();
            let event = enroll(&registry, &keys_a, &ids[0], Device::of(id, "x")).unwrap();
            publish(&drive, &reg_dir, &event, n as u64).unwrap();
        }
        let registry = read_registry(&drive, &reg_dir, None).unwrap();
        let keyrings: Vec<Keyring> = ids
            .iter()
            .map(|id| Keyring::from_registry(&registry, id).unwrap())
            .collect();
        let start = |n: usize| {
            let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
            let board = keyrings[n].board_key();
            engine
                .unlock(&ids[n], &root.join(format!("data-{n}")), Some(&board))
                .unwrap();
            engine
        };
        let mut engines: Vec<Engine> = (0..3).map(start).collect();
        let round = |engines: &[Engine], n: usize| {
            Syncer {
                engine: &engines[n],
                drive: &drive,
                identity: &ids[n],
                registry: &registry,
                keyring: &keyrings[n],
                staging: root.join(format!("staging-{n}")),
            }
            .round()
            .unwrap()
        };
        let settle = |engines: &[Engine]| {
            for _ in 0..3 {
                for n in 0..3 {
                    round(engines, n);
                }
            }
        };
        let view = |e: &Engine| {
            let mut airframes: Vec<String> = e
                .call("airframe.list", json!({}))
                .unwrap()
                .as_array()
                .unwrap()
                .iter()
                .map(|a| format!("{}={}", a["id"], a["name"]))
                .collect();
            let mut flights: Vec<String> = e
                .call("flight.list", json!({}))
                .unwrap()
                .as_array()
                .unwrap()
                .iter()
                .map(|f| f["id"].to_string())
                .collect();
            airframes.sort();
            flights.sort();
            (airframes, flights)
        }; // fmt: skip

        // A imports a log; B and C get its records and the log, and analyse it
        let a = &engines[0];
        let platform = a
            .call(
                "platform.create",
                json!({ "name": "Heli", "vehicle": "copter" }),
            )
            .unwrap();
        let airframe = a
            .call(
                "airframe.create",
                json!({ "platform_id": platform["id"], "name": "H-1" }),
            )
            .unwrap();
        let path = fixture().to_string_lossy().into_owned();
        let scanned = a.call("import.scan", json!({ "paths": [path] })).unwrap();
        let done = a
            .call(
                "import.commit",
                json!({ "batch": scanned["batch"],
            "decisions": [{ "log": "f1", "airframe_id": airframe["id"] }] }),
            )
            .unwrap(); // fmt: skip
        let flight = done[0]["flights"][0].clone();
        settle(&engines);
        let want = view(&engines[0]);
        assert_eq!(want.1.len(), 1);
        for e in &engines[1..] {
            assert_eq!(view(e), want);
            let status = e.call("sync.status", json!({})).unwrap();
            assert_eq!(status["quarantine"], json!([]), "{status}");
            assert_eq!(status["conflicts"], 0);
            let analysis = e
                .call("analysis.run", json!({ "flight_id": flight }))
                .unwrap();
            assert!(analysis["findings"].as_array().is_some());
        }

        // an object held back leaves a gap on C, and the object after it waits
        let p2 = engines[0]
            .call(
                "platform.create",
                json!({ "name": "Quad", "vehicle": "copter" }),
            )
            .unwrap();
        round(&engines, 0);
        let a_folder = format!("devices/{}/", ids[0].laptop_id_hex());
        let held = drive.list(&a_folder).unwrap().into_iter().last().unwrap(); // A's newest
        let aside = root.join("held.fio");
        std::fs::rename(root.join("drive").join(&held.path), &aside).unwrap();
        engines[0]
            .call(
                "airframe.create",
                json!({ "platform_id": p2["id"], "name": "Q-1" }),
            )
            .unwrap();
        round(&engines, 0);
        let r = round(&engines, 2);
        assert_eq!(r.waiting, 1, "{r:?}");
        let seq = crate::sync::object::Name::parse(&held.path)
            .unwrap()
            .sequence;
        assert_eq!(r.gaps, vec![(ids[0].laptop_id_hex(), seq)]);
        std::fs::rename(&aside, root.join("drive").join(&held.path)).unwrap();

        // B's engine dies; a new one carries on from the same store
        engines[1].kill();
        engines[1] = start(1);
        settle(&engines);
        let want = view(&engines[0]);
        assert_eq!(want.0.len(), 2);
        for e in &engines[1..] {
            assert_eq!(view(e), want);
        }
        assert_eq!(round(&engines, 2).gaps, vec![]);
        let status = engines[2].call("sync.status", json!({})).unwrap();
        assert!(
            status["laptops"]
                .as_array()
                .unwrap()
                .iter()
                .all(|l| l["gaps"] == json!([])),
            "{status}"
        );
        for e in &engines {
            e.stop();
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Two laptops through Google Drive, as far as a test can reach it: the client
    /// against the fake Google server, the loop watching the change feed.
    #[test]
    fn two_laptops_converge_through_google_drive() {
        use crate::sync::fake_google::{DRIVE, FakeGoogle};
        use crate::sync::google::GoogleDrive;
        use crate::sync::keyring::{Device, Keyring, create_fleet, enroll};
        use crate::sync::oauth::Client;
        use crate::sync::run::{Calls, Syncer, publish, publish_next, read_registry};

        let Some(cmd) = real_engine() else {
            eprintln!("FI_ENGINE_CMD not set: skipped");
            return;
        };
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let root = std::env::temp_dir().join(format!("fi-sync-google-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let fake = FakeGoogle::start();
        let client = Client {
            id: "test".into(),
            secret: "s".into(),
        };
        let drive = || {
            GoogleDrive::new(
                fake.endpoints(),
                client.clone(),
                DRIVE,
                zeroize::Zeroizing::new(fake.refresh_token()),
            )
            .unwrap()
        };
        let drives = [drive(), drive()];
        let ids: Vec<LaptopIdentity> = (0..2)
            .map(|_| {
                LaptopIdentity::load_or_create(&MemoryStore::default())
                    .unwrap()
                    .0
            })
            .collect();
        let reg_dir = root.join("registry");
        let (first, keys_a, _) = create_fleet(&ids[0], "A").unwrap();
        publish(&drives[0], &reg_dir, &first, 0).unwrap();
        let fp = read_registry(&drives[0], &reg_dir, None)
            .unwrap()
            .fingerprint;
        let registry = publish_next(&drives[0], &reg_dir, &fp, &|r| {
            enroll(r, &keys_a, &ids[0], Device::of(&ids[1], "B"))
        })
        .unwrap();
        // B reads the same registry through its own client
        assert_eq!(
            read_registry(&drives[1], &reg_dir, Some(&fp))
                .unwrap()
                .events
                .len(),
            2
        );
        let keyrings: Vec<Keyring> = ids
            .iter()
            .map(|id| Keyring::from_registry(&registry, id).unwrap())
            .collect();
        let engines: Vec<Engine> = (0..2)
            .map(|n| {
                let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
                engine
                    .unlock(
                        &ids[n],
                        &root.join(format!("data-{n}")),
                        Some(&keyrings[n].board_key()),
                    )
                    .unwrap();
                engine
            })
            .collect();
        let a = &engines[0];
        let platform = a
            .call(
                "platform.create",
                json!({ "name": "Heli", "vehicle": "copter" }),
            )
            .unwrap();
        let airframe = a
            .call(
                "airframe.create",
                json!({ "platform_id": platform["id"], "name": "H-1" }),
            )
            .unwrap();
        let path = fixture().to_string_lossy().into_owned();
        let scanned = a.call("import.scan", json!({ "paths": [path] })).unwrap();
        let done = a
            .call(
                "import.commit",
                json!({ "batch": scanned["batch"],
            "decisions": [{ "log": "f1", "airframe_id": airframe["id"] }] }),
            )
            .unwrap(); // fmt: skip
        let flight = done[0]["flights"][0].clone();
        let mut cursors = [None, None];
        let mut quiet = 0;
        for _ in 0..8 {
            let mut moved = false;
            for n in 0..2 {
                let r = Syncer {
                    engine: &engines[n],
                    drive: &drives[n],
                    identity: &ids[n],
                    registry: &registry,
                    keyring: &keyrings[n],
                    staging: root.join(format!("staging-{n}")),
                }
                .round_watching(&mut cursors[n], false)
                .unwrap();
                moved |= r.uploaded > 0 || r.applied > 0;
            }
            quiet = if moved { 0 } else { quiet + 1 };
            if quiet == 2 {
                break;
            }
        }
        assert_eq!(quiet, 2, "sync settled");
        let digest = |e: &Engine| e.call("sync.digest", json!({})).unwrap()["sha256"].clone();
        assert_eq!(digest(&engines[0]), digest(&engines[1]));
        let analysis = engines[1]
            .call("analysis.run", json!({ "flight_id": flight }))
            .unwrap();
        assert!(analysis["findings"].as_array().is_some());
        // only the shared drive's own folder was used, and nothing was ever deleted
        assert!(
            fake.state
                .lock()
                .unwrap()
                .files
                .values()
                .any(|f| f.name == crate::sync::google::ROOT)
        );
        for e in &engines {
            e.stop();
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn joining_a_fleet_hashes_board_ids_again_once() {
        let Some(cmd) = real_engine() else {
            eprintln!("FI_ENGINE_CMD not set: skipped");
            return;
        };
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let data = std::env::temp_dir().join(format!("fi-shell-fleet-{}", std::process::id()));
        let (identity, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        let fleet: Secret32 = zeroize::Zeroizing::new([7u8; 32]);
        for (board, rekey) in [(None, false), (Some(&fleet), true), (Some(&fleet), false)] {
            let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
            let unlocked = engine.unlock(&identity, &data, board).unwrap();
            // the re-key ran inside unlock when it was needed
            assert_eq!(unlocked["rekey_needed"], json!(rekey));
            engine.stop();
        }
        let _ = std::fs::remove_dir_all(&data);
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
        let unlocked = engine.unlock(&identity, &data, None).unwrap();
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
        engine.unlock(&identity, &data, None).unwrap();
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

    #[test]
    fn a_real_packet_goes_out_through_both_checks_and_comes_back_checked() {
        use crate::claude::tests::{fake_api, message};
        let Some(cmd) = real_engine() else {
            eprintln!("FI_ENGINE_CMD not set: skipped");
            return;
        };
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
        let data = std::env::temp_dir().join(format!("fi-shell-send-{}", std::process::id()));
        let (identity, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        engine.unlock(&identity, &data, None).unwrap();
        let call = |method: &str, params: Value| engine.client.call(method, params, CALL_TIMEOUT);
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
            json!({ "batch": scanned["batch"],
                    "decisions": [{ "log": "f1", "airframe_id": airframe["id"] }] }),
        )
        .unwrap();
        let flight = committed[0]["flights"][0].as_str().unwrap().to_owned();
        let reply = json!({
            "summary": "The hover is clean.", "root_causes": [], "recommendations": [],
            "disagreements": [], "questions": [], "tests_to_fly": [],
        })
        .to_string();
        let fake = fake_api(vec![
            (
                200,
                vec![],
                message(
                    "tool_use",
                    json!([{"type": "tool_use", "id": "tu1",
                "name": "parameter_doc", "input": {"name": "INS_HNTCH_MODE"}}]),
                ),
            ),
            (
                200,
                vec![],
                message("end_turn", json!([{"type": "text", "text": reply}])),
            ),
        ]);
        let mut api =
            crate::claude::Api::new(&fake.url, zeroize::Zeroizing::new("sk-ant-test".into()))
                .unwrap();
        api.backoff = vec![Duration::from_millis(5)];
        let job = crate::claude::Job {
            engine: &engine,
            api: &api,
            model: crate::claude::ANALYSIS,
            language: "uk",
            budget: Some((0.0, 20.0)),
        };
        let out = crate::claude::analyse(&job, &flight).unwrap();
        let crate::claude::Outcome::Done { exchange } = out else {
            panic!("{out:?}")
        };
        assert_eq!(exchange["outcome"], "accepted", "{exchange}");
        let sent = call("ai.sent", json!({ "limit": 10 })).unwrap();
        let rows = sent.as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert!(
            rows.iter()
                .all(|r| r["sent"] == true && r["shell_verdict"] == "pass")
        );
        // Exactly the recorded texts went out, beside the static parts; no position did.
        let seen = fake.seen.lock().unwrap();
        let first: Value = serde_json::from_str(&seen[0].1).unwrap();
        let packet = rows.iter().find(|r| r["purpose"] == "analysis").unwrap();
        assert_eq!(
            first["messages"][0]["content"][1]["text"],
            packet["packet_json"]
        );
        assert!(
            first["messages"][0]["content"][0]["text"]
                .as_str()
                .unwrap()
                .contains("українською")
        );
        let second: Value = serde_json::from_str(&seen[1].1).unwrap();
        let tool = rows.iter().find(|r| r["purpose"] == "tool").unwrap();
        assert_eq!(
            second["messages"][2]["content"][0]["content"],
            tool["packet_json"]
        );
        let prepared_again = call("ai.prepare", json!({ "flight_id": flight })).unwrap();
        for (lat, lon) in
            serde_json::from_value::<Vec<(f64, f64)>>(prepared_again["tripwire"].clone()).unwrap()
        {
            for (_, body) in seen.iter() {
                assert!(
                    !body.contains(&format!("{lat:.4}")) && !body.contains(&format!("{lon:.4}"))
                );
            }
        }
        assert!(
            (call("ai.spend", json!({})).unwrap()["cost_usd"]
                .as_f64()
                .unwrap())
                > 0.0
        );
        engine.stop();
        let _ = std::fs::remove_dir_all(data);
    }

    /// The live smoke test (`.github/workflows/ai-smoke.yml`): one fixture flight's
    /// analysis and one chat turn against the real Claude API, with a 2 USD cap. Needs
    /// the real engine and ANTHROPIC_API_KEY; prints usage and cost, never the key.
    #[test]
    #[ignore = "calls the real Claude API: run by the smoke-test workflow"]
    fn live_claude_smoke() {
        let Some(cmd) = real_engine() else {
            panic!("FI_ENGINE_CMD must name the engine");
        };
        let key = zeroize::Zeroizing::new(
            std::env::var("ANTHROPIC_API_KEY").expect("ANTHROPIC_API_KEY must be set"),
        );
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
        let data = std::env::temp_dir().join(format!("fi-shell-live-{}", std::process::id()));
        let (identity, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        engine.unlock(&identity, &data, None).unwrap();
        let call = |method: &str, params: Value| engine.client.call(method, params, CALL_TIMEOUT);
        let (gate, failed) =
            crate::scrub::gate(call("ai.selftest", json!({})).map_err(|e| e.to_string()));
        assert_eq!((gate, failed), (crate::scrub::AiGate::Ready, vec![]));
        let platform = call(
            "platform.create",
            json!({ "name": "Smoke", "vehicle": "copter" }),
        )
        .unwrap();
        let airframe = call(
            "airframe.create",
            json!({ "platform_id": platform["id"], "name": "S1" }),
        )
        .unwrap();
        let log = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/sitl/4.7.0/copter-heavylift-loaded.bin");
        let scanned = call("import.scan", json!({ "paths": [log.to_string_lossy()] })).unwrap();
        let committed = call(
            "import.commit",
            json!({ "batch": scanned["batch"],
                    "decisions": [{ "log": "f1", "airframe_id": airframe["id"] }] }),
        )
        .unwrap();
        let flight = committed[0]["flights"][0].as_str().unwrap().to_owned();
        call("analysis.run", json!({ "flight_id": flight })).unwrap();
        let api = crate::claude::Api::new(crate::claude::API, key).unwrap();

        let job = crate::claude::Job {
            engine: &engine,
            api: &api,
            model: crate::claude::ANALYSIS,
            language: "en",
            budget: Some((0.0, 2.0)),
        };
        let out = crate::claude::analyse(&job, &flight).unwrap();
        let crate::claude::Outcome::Done { exchange } = out else {
            panic!("not sent: {out:?}")
        };
        println!(
            "analysis: outcome {}, model {}, {} tool calls, {:.4} USD, verdicts {}",
            exchange["outcome"],
            exchange["model"],
            exchange["tool_calls"],
            exchange["cost_usd"].as_f64().unwrap_or(0.0),
            exchange["recommendations"]
        );
        assert_eq!(exchange["outcome"], "accepted", "{}", exchange["reasons"]);

        let chat = crate::claude::Job {
            model: crate::claude::CHAT,
            budget: None,
            ..job
        };
        let prepared = crate::claude::prepare(
            &engine,
            &flight,
            "chat",
            Some("What should be fixed first, and why?"),
        )
        .unwrap()
        .unwrap();
        let crate::claude::Outcome::Done { exchange } =
            crate::claude::send(&chat, prepared).unwrap()
        else {
            panic!("the chat turn was not sent");
        };
        println!(
            "chat: outcome {}, model {}, {:.4} USD",
            exchange["outcome"],
            exchange["model"],
            exchange["cost_usd"].as_f64().unwrap_or(0.0)
        );
        assert_eq!(exchange["outcome"], "accepted", "{}", exchange["reasons"]);
        let sent = call("ai.sent", json!({ "limit": 100 })).unwrap();
        let rows = sent.as_array().unwrap();
        println!(
            "packets: {} built, {} sent",
            rows.len(),
            rows.iter().filter(|r| r["sent"] == true).count()
        );
        assert!(
            rows.iter()
                .all(|r| r["sent"] == false || r["shell_verdict"] == "pass")
        );
        engine.stop();
        let _ = std::fs::remove_dir_all(data);
    }

    /// Every grid point's series, and how many of their values are missing.
    fn grid_reply(body: &[u8], hourly: &[&str]) -> (usize, usize, usize, usize) {
        let v: Value = serde_json::from_slice(body).expect("the reply is JSON");
        // one location comes as an object, several as a list
        let points = match v {
            Value::Array(a) => a,
            one => vec![one],
        };
        let (mut hours, mut values, mut missing) = (0, 0, 0);
        for p in &points {
            let h = &p["hourly"];
            let times = h["time"].as_array().expect("hourly times").len();
            assert!(times > 0, "a point with no hours");
            hours = hours.max(times);
            for name in hourly {
                let series = h[*name]
                    .as_array()
                    .unwrap_or_else(|| panic!("no {name} series"));
                assert_eq!(series.len(), times, "{name} does not cover every hour");
                values += series.len();
                missing += series.iter().filter(|x| !x.is_number()).count();
            }
        }
        (points.len(), hours, values, missing)
    }

    /// The live weather check (`.github/workflows/weather-check.yml`): one request of each
    /// kind the downloader sends (the newest forecast window, one day of ERA5 and the
    /// K-index), made by the downloader's own code for one tile of a fixture flight,
    /// against the real Open-Meteo and NOAA SWPC with OPEN_METEO_API_KEY. Each reply must
    /// hold every variable at every grid point the request named, and the engine must keep
    /// it. Prints sizes and counts only: never the key, an address or the tile.
    #[test]
    #[ignore = "calls the real Open-Meteo and NOAA SWPC: run by the weather-check workflow"]
    fn live_weather_check() {
        use crate::weather::{fetch, plan};
        let Some(cmd) = real_engine() else {
            panic!("FI_ENGINE_CMD must name the engine");
        };
        let key = zeroize::Zeroizing::new(
            std::env::var("OPEN_METEO_API_KEY").expect("OPEN_METEO_API_KEY must be set"),
        );
        assert!(
            crate::weather::key_form_ok(&key),
            "the key is not in the form Open-Meteo issues"
        );
        let knowledge = std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from);
        let (engine, _) = Engine::start(&cmd, knowledge.as_deref(), |_, _| {}).unwrap();
        let data = std::env::temp_dir().join(format!("fi-shell-weather-{}", std::process::id()));
        let (identity, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        engine.unlock(&identity, &data, None).unwrap();
        let call = |method: &str, params: Value| engine.client.call(method, params, CALL_TIMEOUT);
        let platform = call(
            "platform.create",
            json!({ "name": "Weather", "vehicle": "copter" }),
        )
        .unwrap();
        let airframe = call(
            "airframe.create",
            json!({ "platform_id": platform["id"], "name": "W1" }),
        )
        .unwrap();
        let log = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/sitl/4.7.0/copter-heavylift-loaded.bin");
        let scanned = call("import.scan", json!({ "paths": [log.to_string_lossy()] })).unwrap();
        call(
            "import.commit",
            json!({ "batch": scanned["batch"],
                    "decisions": [{ "log": "f1", "airframe_id": airframe["id"] }] }),
        )
        .unwrap();

        // The fixture's own tile, listed as if long ago, so its delay is past.
        let (tiles, grids) = fetch::tiles_and_grids(&engine).unwrap();
        let tile = plan::Tile {
            name: tiles
                .first()
                .expect("the fixture names a tile")
                .name
                .clone(),
            added_ms: 0,
        };
        let schedule = plan::Schedule {
            offset_min: 0,
            seed: [7; 32],
        };
        let slot = schedule.slot_at(crate::weather::now_ms());
        let due = plan::due(slot, &schedule, &[tile], &plan::Held::new(), &grids);
        let newest = |source: plan::Source| {
            due.iter()
                .filter(|r| r.source == source)
                // the newest, and for reanalysis a single day rather than a month
                .max_by_key(|r| (r.period.len(), r.period.clone()))
                .cloned()
                .unwrap_or_else(|| panic!("nothing due from {}", source.name()))
        };
        let http = crate::net::client(Duration::from_secs(120)).unwrap();
        let hosts = fetch::Hosts::default();
        let staging = data.join("weather-staging");
        std::fs::create_dir_all(&staging).unwrap();
        for (source, who, hourly) in [
            (plan::Source::Forecast, "Open-Meteo", plan::FORECAST_HOURLY),
            (
                plan::Source::Reanalysis,
                "Open-Meteo",
                plan::REANALYSIS_HOURLY,
            ),
            (plan::Source::Kp, "NOAA SWPC", &[][..]),
        ] {
            let r = newest(source);
            let started = Instant::now();
            let url = fetch::address(&hosts, &r, &key).unwrap();
            let body = fetch::fetch(&http, url.clone(), who).unwrap_or_else(|stop| {
                // Ask once more to say why: the status and the provider's own reason,
                // with the key masked (the URL is never printed).
                let why = http.get(url).send().map_or_else(
                    |e| format!("no reply: {}", e.without_url()),
                    |reply| {
                        let status = reply.status();
                        let text = reply
                            .text()
                            .unwrap_or_default()
                            .replace(key.as_str(), "***");
                        let reason = serde_json::from_str::<Value>(&text)
                            .ok()
                            .and_then(|v| v["reason"].as_str().map(str::to_owned))
                            .unwrap_or(text);
                        format!("{status}: {}", reason.chars().take(300).collect::<String>())
                    },
                );
                panic!("{}: {stop} ({why})", source.name())
            });
            let seconds = started.elapsed().as_secs_f64();
            if source == plan::Source::Kp {
                let rows: Value = serde_json::from_slice(&body).expect("the K-index is JSON");
                let n = rows.as_array().map_or(0, Vec::len);
                assert!(n > 1, "no K-index rows");
                println!(
                    "{}: {} bytes in {seconds:.1} s, {n} rows",
                    source.name(),
                    body.len()
                );
            } else {
                let asked = r
                    .query
                    .iter()
                    .find(|(k, _)| k == "latitude")
                    .map_or(0, |(_, v)| v.split(',').count());
                let (points, hours, values, missing) = grid_reply(&body, hourly);
                assert_eq!(points, asked, "a reply for every grid point asked");
                println!(
                    "{}: {} bytes in {seconds:.1} s, {points} points, {hours} hours, {} \
                     variables, {missing} of {values} values missing",
                    source.name(),
                    body.len(),
                    hourly.len()
                );
                assert!(
                    missing * 20 <= values,
                    "more than 5% of the values are missing"
                );
            }
            let file = staging.join(format!("{}.json", source.name()));
            std::fs::write(&file, &body).unwrap();
            call(
                "weather.store",
                json!({ "source": source.name(), "tile": r.tile, "period": r.period,
                        "path": file.to_string_lossy() }),
            )
            .unwrap_or_else(|e| panic!("{}: the engine did not keep it: {e}", source.name()));
            let held = call(
                "weather.held",
                json!({ "source": source.name(), "since": "2000" }),
            )
            .unwrap();
            assert!(
                held["held"].as_array().is_some_and(|rows| rows
                    .iter()
                    .any(|row| row[0] == r.tile.as_str() && row[1] == r.period.as_str())),
                "{}: not listed as held",
                source.name()
            );
        }
        engine.stop();
        let _ = std::fs::remove_dir_all(data);
    }
}
