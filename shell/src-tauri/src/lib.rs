//! Flight Insights desktop shell: windows, the engine sidecar, the OS keychain,
//! sync and outbound requests (see docs/SPEC.md, "System architecture").
//!
//! At start-up the shell reads (or on first start creates) this laptop's
//! identity in the OS keychain, starts the engine, and unlocks the engine's
//! encrypted store with the keys. The UI talks to the engine only through
//! `engine_call`, limited to `engine::UI_METHODS`. Each engine started runs the
//! location checks' self-test (`scrub::gate`); AI calls stay blocked unless it passes.
//! Its network endpoints are the Claude API (`net`, `claude`), reached from the AI
//! worker, and the Google Drive API, reached from the sync worker (`fleet`, `sync`),
//! which shares this laptop's records with the fleet every minute.

pub mod ai;
pub mod claude;
pub mod engine;
mod fleet;
pub mod identity;
pub mod net;
pub mod rpc;
pub mod scrub;
pub mod station;
pub mod sync;
pub mod watch;
pub mod weather;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, RunEvent};

use engine::{CALL_TIMEOUT, Engine, EngineCommand, UI_METHODS};
use identity::{LaptopIdentity, OsKeychain, SecretStore};

#[derive(Clone, Serialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum EngineState {
    #[default]
    Starting,
    Ready,
    Failed,
}

#[derive(Clone, Serialize, Default)]
pub struct Status {
    pub laptop: Option<String>,
    pub signing_public: Option<String>,
    pub encryption_public: Option<String>,
    pub first_start: bool,
    pub engine: EngineState,
    pub engine_version: Option<String>,
    pub error: Option<String>,
    /// AI calls are allowed only when `ready`: the start-up self-test of both location
    /// checks, the engine's and the shell's, passed.
    pub ai: scrub::AiGate,
    /// The self-test cases that failed, by name.
    pub ai_failed: Vec<String>,
}

struct Shell {
    watch: Mutex<watch::Watcher>,
    keychain: Mutex<Option<Arc<dyn SecretStore>>>,
    queue: ai::Queue,
    /// Set when the API refused the key; cleared when a new key is set.
    key_rejected: AtomicBool,
    /// Chat turns built and checked, waiting for the person's review; never written down.
    chats: Mutex<std::collections::HashMap<String, claude::Prepared>>,
    identity: Mutex<Option<Arc<LaptopIdentity>>>,
    engine: Mutex<Option<Arc<Engine>>>,
    /// The sync worker's state, and what wakes it.
    sync: sync::app::Worker,
    /// The weather downloader's state.
    weather: weather::Worker,
    /// The signed-in shared drive, by its id.
    drive: Mutex<Option<(String, Arc<sync::google::GoogleDrive>)>>,
    /// The device registry as last read, for the sync panel.
    registry: Mutex<Option<sync::keyring::Registry>>,
    /// Held while the engine is started, so only one is ever started at a time.
    starting: Mutex<()>,
    /// Set when the app exits: no engine is started after that.
    exiting: AtomicBool,
    status: Mutex<Status>,
    command: Mutex<Option<EngineCommand>>,
    knowledge_dir: Option<PathBuf>,
    data_dir: PathBuf,
}

impl Shell {
    fn set_failed(&self, error: String) {
        let mut s = self.status.lock().expect("status");
        s.engine = EngineState::Failed;
        s.error = Some(error);
        // No engine has been checked: AI calls wait for one that is.
        s.ai = scrub::AiGate::Pending;
        s.ai_failed.clear();
    }

    /// Start the engine and unlock it; replaces a stopped one.
    fn start_engine(&self, app: &AppHandle) -> Result<Arc<Engine>, String> {
        let identity = self
            .identity
            .lock()
            .expect("identity")
            .clone()
            .ok_or("the laptop identity is not available")?;
        let cmd = self
            .command
            .lock()
            .expect("command")
            .clone()
            .ok_or("no engine to start")?;
        let events = app.clone();
        let (engine, hello) = Engine::start(&cmd, self.knowledge_dir.as_deref(), move |m, p| {
            if m == "$/progress" {
                let _ = events.emit("engine-progress", p.clone());
            }
        })?;
        // In a fleet, board IDs are hashed with the fleet's key. Damaged fleet keys stop
        // the start: hashing with this laptop's own key would undo the fleet's hashes.
        let keychain = self.keychain.lock().expect("keychain").clone();
        let fleet = match keychain {
            Some(k) => sync::keyring::Keyring::load(k.as_ref())?,
            None => None,
        };
        let board = fleet.as_ref().map(sync::keyring::Keyring::board_key);
        engine
            .unlock(&identity, &self.data_dir, board.as_ref())
            .map_err(|e| format!("the engine could not open the local store: {e}"))?;
        // Checked again with every engine started, so a restarted one is checked too.
        let (ai, ai_failed) = scrub::gate(
            engine
                .client
                .call("ai.selftest", serde_json::json!({}), CALL_TIMEOUT)
                .map_err(|e| e.to_string()),
        );
        for f in &ai_failed {
            eprintln!("location check self-test: {f}");
        }
        let engine = Arc::new(engine);
        *self.engine.lock().expect("engine") = Some(engine.clone());
        let mut s = self.status.lock().expect("status");
        s.engine = EngineState::Ready;
        s.engine_version = hello["engine"].as_str().map(str::to_owned);
        s.error = None;
        s.ai = ai;
        s.ai_failed = ai_failed;
        Ok(engine)
    }

    /// The engine, if it is running.
    fn current_engine(&self) -> Option<Arc<Engine>> {
        self.engine
            .lock()
            .expect("engine")
            .clone()
            .filter(|e| e.is_running())
    }

    fn running_engine(&self, app: &AppHandle) -> Result<Arc<Engine>, String> {
        if let Some(e) = self.current_engine() {
            return Ok(e);
        }
        let _starting = self.starting.lock().expect("engine start");
        // Another call, or start-up, may have started one while this one waited.
        if let Some(e) = self.current_engine() {
            return Ok(e);
        }
        if self.exiting.load(Ordering::SeqCst) {
            return Err("Flight Insights is closing".into());
        }
        if self.command.lock().expect("command").is_none() {
            // Start-up is still reading the keychain, or failed before the engine
            // could be started and said why.
            let error = self.status.lock().expect("status").error.clone();
            return Err(error.unwrap_or_else(|| "the engine is still starting".into()));
        }
        // The engine stopped (crashed or was killed): start a fresh one.
        let _ = app.emit("engine-restarting", ());
        let started = self.start_engine(app);
        if let Err(e) = &started {
            self.set_failed(e.clone());
        }
        let _ = app.emit("shell-status", self.status.lock().expect("status").clone());
        started
    }
}

#[tauri::command]
fn shell_status(shell: tauri::State<'_, Shell>) -> Status {
    shell.status.lock().expect("status").clone()
}

#[tauri::command]
async fn engine_call(
    app: AppHandle,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    if !UI_METHODS.contains(&method.as_str()) {
        return Err(format!("{method} is not available to the interface"));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let engine = shell.running_engine(&app)?;
        let params = params.unwrap_or_else(|| Value::Object(Default::default()));
        let flight = params["flight_id"].as_str().map(str::to_owned);
        let reply = engine
            .client
            .call(&method, params, CALL_TIMEOUT)
            .map_err(|e| e.to_string())?;
        // A new import goes to the other laptops without waiting for the minute.
        if method == "import.commit" {
            shell.sync.wake();
        }
        // Every analysis gets its AI pass, in the background (spec: AI layer).
        if let (true, Some(flight)) = (method == "analysis.run", flight) {
            let run = reply["run"].as_str().map(str::to_owned);
            shell.queue.push(&flight, run, false);
        }
        Ok(reply)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Read this computer's facts and record them on a ground station's profile.
#[tauri::command]
async fn detect_this_computer(app: AppHandle, id: String, version: i64) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let engine = shell.running_engine(&app)?;
        let facts = station::computer_facts(&shell.data_dir);
        let params = serde_json::json!({ "id": id, "version": version, "facts": facts });
        engine
            .client
            .call("station.record_computer", params, CALL_TIMEOUT)
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Ask the engine which of `paths` are new, and offer those. Returns whether the
/// offered files changed. Only a running engine is asked; the watcher never starts one.
/// A chunk the engine cannot answer is left unchecked, to be asked about next poll,
/// and the rest are still checked.
fn check_files(app: &AppHandle, ask: Vec<PathBuf>) -> bool {
    let shell = app.state::<Shell>();
    let Some(engine) = shell.current_engine() else {
        return false;
    };
    let mut asked: Vec<PathBuf> = Vec::new();
    let mut new: Vec<String> = Vec::new();
    for chunk in ask.chunks(500) {
        let paths: Vec<String> = chunk
            .iter()
            .map(|p| p.to_string_lossy().into_owned())
            .collect();
        let reply = engine
            .client
            .call(
                "import.unknown",
                serde_json::json!({ "paths": paths }),
                CALL_TIMEOUT,
            )
            .map_err(|e| e.to_string())
            .and_then(|r| serde_json::from_value::<Vec<String>>(r).map_err(|e| e.to_string()));
        match reply {
            Ok(n) => {
                asked.extend_from_slice(chunk);
                new.extend(n);
            }
            Err(e) => eprintln!("watched folders: {e}"),
        }
    }
    let mut w = shell.watch.lock().expect("watcher");
    let before = w.pending().to_vec();
    w.checked(&asked, &new);
    w.pending() != before.as_slice()
}

/// One poll of the watched folders.
fn poll_folders(app: &AppHandle) -> Result<(), String> {
    let shell = app.state::<Shell>();
    let Some(engine) = shell.current_engine() else {
        return Ok(());
    };
    let reply = engine
        .client
        .call("station.folders", serde_json::json!({}), CALL_TIMEOUT)
        .map_err(|e| e.to_string())?;
    let folders: Vec<watch::Folder> = serde_json::from_value(reply).map_err(|e| e.to_string())?;
    // The walk can be long; the import dialog waits on the watcher's lock.
    let scanned = watch::scan(&folders);
    let (ready, before) = {
        let mut w = shell.watch.lock().expect("watcher");
        let before = w.pending().to_vec();
        (w.update(scanned), before)
    };
    let changed = !ready.is_empty() && check_files(app, ready);
    let pending = shell.watch.lock().expect("watcher").pending().to_vec();
    if changed || pending != before {
        let _ = app.emit("import-pending", pending);
    }
    Ok(())
}

/// New files found in watched folders, waiting to be offered. With `refresh`, the
/// engine is asked again first, after an import or a dismissal.
#[tauri::command]
async fn pending_imports(app: AppHandle, refresh: bool) -> Result<Vec<watch::Pending>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if refresh {
            let ask = app
                .state::<Shell>()
                .watch
                .lock()
                .expect("watcher")
                .recheck();
            if !ask.is_empty() {
                check_files(&app, ask);
            }
        }
        Ok(app
            .state::<Shell>()
            .watch
            .lock()
            .expect("watcher")
            .pending()
            .to_vec())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// One flight's AI pass, from the queue. What it came to is sent to the interface as
/// `ai-progress`; nothing here blocks anything else.
fn ai_pass(app: &AppHandle, item: ai::Item) {
    let shell = app.state::<Shell>();
    let emit = |state: &str, detail: Value| {
        let _ = app.emit(
            "ai-progress",
            serde_json::json!({ "flight_id": item.flight_id, "state": state, "detail": detail }),
        );
    };
    if shell.status.lock().expect("status").ai != scrub::AiGate::Ready {
        return emit("gate", Value::Null);
    }
    let Some(engine) = shell.current_engine() else {
        shell.queue.retry(item.clone());
        return;
    };
    let keychain = shell.keychain.lock().expect("keychain").clone();
    let key = match keychain.map(|k| ai::key(k.as_ref())) {
        Some(Ok(Some(key))) => key,
        _ => return emit("no_key", Value::Null),
    };
    if !item.forced {
        // Answered already for this analysis: not sent again unless a person asks.
        let done = claude::Engine::call(
            engine.as_ref(),
            "ai.results",
            serde_json::json!({ "flight_id": item.flight_id }),
        );
        if let Ok(last) = done
            && !last.is_null()
            && last["outcome"] != "failed"
            && (item.run_id.is_none() || last["run_id"].as_str() == item.run_id.as_deref())
        {
            return;
        }
    }
    let settings = ai::load_settings(&shell.data_dir);
    let budget = if item.forced {
        None
    } else {
        let spent = claude::Engine::call(engine.as_ref(), "ai.spend", serde_json::json!({}))
            .ok()
            .and_then(|s| s["cost_usd"].as_f64());
        match spent {
            Some(spent) => Some((spent, settings.monthly_cap_usd)),
            None => {
                return emit(
                    "failed",
                    serde_json::json!("the month's spend could not be read"),
                );
            }
        }
    };
    let api = match claude::Api::new(claude::API, key) {
        Ok(a) => a,
        Err(e) => return emit("failed", serde_json::json!(e)),
    };
    let job = claude::Job {
        engine: engine.as_ref(),
        api: &api,
        model: claude::ANALYSIS,
        language: &settings.language,
        budget,
    };
    emit("sending", Value::Null);
    match claude::analyse(&job, &item.flight_id) {
        Ok(claude::Outcome::Done { exchange }) => {
            let reasons = exchange["reasons"].to_string();
            if reasons.contains(&claude::ApiError::Key.to_string()) {
                shell.key_rejected.store(true, Ordering::SeqCst);
            } else if exchange["outcome"] == "failed"
                && reasons.contains("could not be reached")
                && shell.queue.retry(item.clone())
            {
                return emit("waiting", exchange);
            }
            emit("done", exchange);
        }
        Ok(other) => emit("held", serde_json::to_value(other).unwrap_or_default()),
        Err(e) => emit("failed", serde_json::json!(e)),
    }
}

/// What the interface may know of the AI layer: whether a key is set (never the key),
/// the cap, this month's spend, the gate and the queue.
#[tauri::command]
async fn ai_status(app: AppHandle) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let keychain = shell.keychain.lock().expect("keychain").clone();
        let key_set = keychain
            .map(|k| ai::key(k.as_ref()).map(|k| k.is_some()))
            .transpose()?
            .unwrap_or(false);
        let settings = ai::load_settings(&shell.data_dir);
        let spend = shell
            .current_engine()
            .and_then(|e| claude::Engine::call(e.as_ref(), "ai.spend", serde_json::json!({})).ok());
        Ok(serde_json::json!({
            "key_set": key_set,
            "key_rejected": shell.key_rejected.load(Ordering::SeqCst),
            "monthly_cap_usd": settings.monthly_cap_usd,
            "language": settings.language,
            "month": spend,
            "queued": shell.queue.len(),
            "gate": shell.status.lock().expect("status").ai,
        }))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Keeps a Claude API key in the OS keychain. The interface can set or remove it,
/// never read it back.
#[tauri::command]
async fn ai_key_set(app: AppHandle, key: String) -> Result<(), String> {
    let key = zeroize::Zeroizing::new(key);
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let keychain = shell.keychain.lock().expect("keychain").clone();
        let keychain = keychain.ok_or("the OS keychain is not available")?;
        ai::set_key(keychain.as_ref(), &key)?;
        shell.key_rejected.store(false, Ordering::SeqCst);
        // In a fleet, the key is entered once and reaches every laptop (spec: Security,
        // "Service keys").
        if fleet::in_fleet(&shell) {
            shell.sync.share_keys();
        }
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn ai_key_clear(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let keychain = shell.keychain.lock().expect("keychain").clone();
        keychain
            .ok_or("the OS keychain is not available")?
            .delete(ai::KEY_NAME)?;
        if fleet::in_fleet(&shell) {
            shell.sync.share_keys();
        }
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Keeps the Open-Meteo key in the OS keychain. The interface can set or remove it,
/// never read it back; in a fleet it goes to every laptop.
#[tauri::command]
async fn weather_key_set(app: AppHandle, key: String) -> Result<(), String> {
    let key = zeroize::Zeroizing::new(key);
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let keychain = shell.keychain.lock().expect("keychain").clone();
        let keychain = keychain.ok_or("the OS keychain is not available")?;
        weather::set_key(keychain.as_ref(), &key)?;
        if fleet::in_fleet(&shell) {
            shell.sync.share_keys();
        }
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn weather_key_clear(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let keychain = shell.keychain.lock().expect("keychain").clone();
        keychain
            .ok_or("the OS keychain is not available")?
            .delete(weather::KEY_NAME)?;
        if fleet::in_fleet(&shell) {
            shell.sync.share_keys();
        }
        Ok(())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// What the interface may know of weather: whether a key is set (never the key), the
/// last download round's outcome and when the next one is (never a tile or a place).
#[tauri::command]
async fn weather_status(app: AppHandle) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let keychain = shell.keychain.lock().expect("keychain").clone();
        let key_set = keychain
            .map(|k| weather::key(k.as_ref()).map(|k| k.is_some()))
            .transpose()?
            .unwrap_or(false);
        let schedule = weather::load_schedule(&shell.data_dir);
        let mut state = shell.weather.state.lock().expect("weather state").clone();
        if state.is_null() {
            state = serde_json::json!({ "state": "waiting" });
        }
        state["key_set"] = serde_json::json!(key_set);
        state["next_ms"] = serde_json::json!(schedule.next_instant(weather::now_ms()));
        Ok(state)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn ai_settings_set(
    shell: tauri::State<'_, Shell>,
    monthly_cap_usd: f64,
    language: String,
) -> Result<(), String> {
    ai::save_settings(
        &shell.data_dir,
        &ai::Settings {
            monthly_cap_usd,
            language,
        },
    )
}

/// A person asks for a flight's AI pass now: sent even if answered before, and past
/// the monthly cap.
#[tauri::command]
fn ai_send_now(shell: tauri::State<'_, Shell>, flight_id: String) {
    shell.queue.push(&flight_id, None, true);
}

/// A chat question, built into a packet and checked by both checks, for the person to
/// review: the exact text that would be sent comes back, and nothing is sent yet.
#[tauri::command]
async fn ai_chat_prepare(
    app: AppHandle,
    flight_id: String,
    question: String,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        if shell.status.lock().expect("status").ai != scrub::AiGate::Ready {
            return Err("AI calls are blocked until the location check's self-test passes".into());
        }
        let engine = shell.running_engine(&app)?;
        match claude::prepare(engine.as_ref(), &flight_id, "chat", Some(question.trim()))? {
            Err(outcome) => serde_json::to_value(outcome).map_err(|e| e.to_string()),
            Ok(p) => {
                let id = p.packet_id.as_str().unwrap_or_default().to_owned();
                let shown = serde_json::json!({
                    "state": "review", "packet_id": id, "packet_json": p.text,
                });
                shell.chats.lock().expect("chats").insert(id, p);
                Ok(shown)
            }
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// The person read the packet and sends it: the chat model answers.
#[tauri::command]
async fn ai_chat_send(app: AppHandle, packet_id: String) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let prepared = shell
            .chats
            .lock()
            .expect("chats")
            .remove(&packet_id)
            .ok_or("that question was not prepared, or was already sent")?;
        let engine = shell.running_engine(&app)?;
        let keychain = shell.keychain.lock().expect("keychain").clone();
        let key = keychain.map(|k| ai::key(k.as_ref())).transpose()?.flatten();
        let Some(key) = key else {
            claude::withdraw(engine.as_ref(), &prepared)?;
            return Err("no Claude API key is set".into());
        };
        let api = claude::Api::new(claude::API, key)?;
        let settings = ai::load_settings(&shell.data_dir);
        let job = claude::Job {
            engine: engine.as_ref(),
            api: &api,
            model: claude::CHAT,
            language: &settings.language,
            budget: None, // a person read it and asked
        };
        let out = claude::send(&job, prepared)?;
        if let claude::Outcome::Done { exchange } = &out
            && exchange["reasons"]
                .to_string()
                .contains(&claude::ApiError::Key.to_string())
        {
            shell.key_rejected.store(true, Ordering::SeqCst);
        }
        serde_json::to_value(out).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// The person decided not to send it: recorded as not sent.
#[tauri::command]
async fn ai_chat_cancel(app: AppHandle, packet_id: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let shell = app.state::<Shell>();
        let prepared = shell.chats.lock().expect("chats").remove(&packet_id);
        match (prepared, shell.current_engine()) {
            (Some(p), Some(engine)) => claude::withdraw(engine.as_ref(), &p),
            _ => Ok(()),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

/// One slot's weather downloads, when a key is set and the engine is running.
fn weather_round(
    shell: &Shell,
    schedule: &weather::plan::Schedule,
    slot: i64,
    fleet: Option<&fleet::WeatherFleet>,
) {
    let now = weather::now_ms();
    let keychain = shell.keychain.lock().expect("keychain").clone();
    let key = keychain.and_then(|k| weather::key(k.as_ref()).ok().flatten());
    let Some(key) = key else {
        return shell
            .weather
            .record(serde_json::json!({"state": "no key"}), schedule, now);
    };
    let Some(engine) = shell.current_engine() else {
        return shell
            .weather
            .record(serde_json::json!({"state": "no engine"}), schedule, now);
    };
    let staging = weather::share::staging(&shell.data_dir);
    let shared = fleet.map(|f| weather::share::Fleet {
        drive: f.drive.as_ref(),
        registry: &f.registry,
        keyring: &f.keyring,
        identity: f.identity.as_ref(),
        staging: staging.clone(),
    });
    let outcome = net::client(Duration::from_secs(120))
        .map_err(weather::fetch::Stop::Failed)
        .and_then(|http| {
            weather::fetch::round(
                engine.as_ref(),
                &http,
                &weather::fetch::Hosts::default(),
                &key,
                schedule,
                slot,
                &staging,
                &mut |_| {},
                shared.as_ref().map(|s| s as &dyn weather::fetch::Share),
            )
        });
    // Whatever the round kept, even one cut short, goes into the flights' records, so
    // trends and attribution read current weather.
    let _ = engine
        .client
        .call("weather.refresh", serde_json::json!({}), CALL_TIMEOUT);
    shell
        .weather
        .record(weather::fetch::summary(&outcome), schedule, now);
}

fn start_up(app: &AppHandle) {
    let shell = app.state::<Shell>();
    let identifier = app.config().identifier.clone();
    let keychain: Arc<dyn SecretStore> = match OsKeychain::new(&identifier) {
        Ok(k) => Arc::new(k),
        Err(e) => return shell.set_failed(format!("the OS keychain is not available: {e}")),
    };
    *shell.keychain.lock().expect("keychain") = Some(keychain.clone());
    let loaded = LaptopIdentity::load_or_create(keychain.as_ref());
    let (identity, created) = match loaded {
        Ok(v) => v,
        Err(e) => return shell.set_failed(format!("the OS keychain is not available: {e}")),
    };
    {
        let mut s = shell.status.lock().expect("status");
        s.laptop = Some(identity.short_code());
        s.signing_public = Some(hex::encode(identity.signing_public()));
        s.encryption_public = Some(hex::encode(identity.encryption_public()));
        s.first_start = created;
    }
    *shell.identity.lock().expect("identity") = Some(Arc::new(identity));
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(PathBuf::from))
        .unwrap_or_default();
    // Calls made meanwhile wait for this start rather than racing it with their own.
    let starting = shell.starting.lock().expect("engine start");
    if shell.exiting.load(Ordering::SeqCst) {
        return;
    }
    match EngineCommand::resolve(&exe_dir) {
        Ok(cmd) => *shell.command.lock().expect("command") = Some(cmd),
        Err(e) => return shell.set_failed(e),
    }
    if let Err(e) = shell.start_engine(app) {
        shell.set_failed(e);
    }
    drop(starting);
    let _ = app.emit("shell-status", shell.status.lock().expect("status").clone());
    let worker = app.clone();
    let _ = std::thread::Builder::new()
        .name("ai-worker".into())
        .spawn(move || {
            loop {
                if let Some(item) = worker.state::<Shell>().queue.next(Duration::from_secs(60)) {
                    ai_pass(&worker, item);
                }
            }
        });
    let syncer = app.clone();
    let _ = std::thread::Builder::new()
        .name("sync".into())
        .spawn(move || {
            loop {
                fleet::round(&syncer);
                syncer.state::<Shell>().sync.wait();
            }
        });
    let weatherer = app.clone();
    let _ = std::thread::Builder::new()
        .name("weather".into())
        .spawn(move || {
            loop {
                let shell = weatherer.state::<Shell>();
                // in a fleet, the fleet's schedule, so its laptops take turns at one slot
                let fleet = fleet::weather_fleet(&shell);
                let schedule = match &fleet {
                    Some(f) => weather::share::fleet_schedule(&f.keyring.object_keys()),
                    None => weather::load_schedule(&shell.data_dir),
                };
                if let Some(slot) = shell.weather.take_slot(&schedule, weather::now_ms()) {
                    if let Some(f) = &fleet {
                        let members: Vec<String> = f
                            .registry
                            .members
                            .iter()
                            .filter(|(_, m)| m.revoked_after.is_none())
                            .map(|(id, _)| id.clone())
                            .collect();
                        let wait = weather::share::turn_wait_ms(
                            &f.keyring.object_keys(),
                            slot,
                            &f.identity.laptop_id_hex(),
                            &members,
                        );
                        std::thread::sleep(Duration::from_millis(wait as u64));
                    }
                    weather_round(&shell, &schedule, slot, fleet.as_ref());
                }
                let now = weather::now_ms();
                let wait = (schedule.next_instant(now) - now).clamp(1_000, weather::WAKE_MS);
                std::thread::sleep(Duration::from_millis(wait as u64));
            }
        });
    let watcher = app.clone();
    let _ = std::thread::Builder::new()
        .name("folder-watch".into())
        .spawn(move || {
            loop {
                if let Err(e) = poll_folders(&watcher) {
                    eprintln!("watched folders: {e}");
                }
                std::thread::sleep(watch::POLL);
            }
        });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let data_dir = app.path().app_data_dir()?;
            let knowledge_dir = std::env::var_os("FI_KNOWLEDGE_DIR")
                .map(PathBuf::from)
                .or_else(|| app.path().resource_dir().ok().map(|d| d.join("knowledge")));
            app.manage(Shell {
                watch: Mutex::new(watch::Watcher::default()),
                keychain: Mutex::new(None),
                queue: ai::Queue::default(),
                key_rejected: AtomicBool::new(false),
                chats: Mutex::new(Default::default()),
                identity: Mutex::new(None),
                engine: Mutex::new(None),
                sync: sync::app::Worker::default(),
                drive: Mutex::new(None),
                registry: Mutex::new(None),
                starting: Mutex::new(()),
                weather: weather::Worker::default(),
                exiting: AtomicBool::new(false),
                status: Mutex::new(Status::default()),
                command: Mutex::new(None),
                knowledge_dir,
                data_dir,
            });
            let handle = app.handle().clone();
            // The keychain may prompt and the engine takes a moment: not on the UI thread.
            std::thread::spawn(move || start_up(&handle));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            shell_status,
            engine_call,
            detect_this_computer,
            pending_imports,
            ai_status,
            ai_key_set,
            ai_key_clear,
            ai_settings_set,
            ai_send_now,
            ai_chat_prepare,
            ai_chat_send,
            ai_chat_cancel,
            weather_status,
            weather_key_set,
            weather_key_clear,
            fleet::sync_status,
            fleet::sync_now,
            fleet::sync_choose_drive,
            fleet::google_sign_in,
            fleet::google_sign_out,
            fleet::google_drives,
            fleet::fleet_create,
            fleet::fleet_code,
            fleet::fleet_find,
            fleet::fleet_join,
            fleet::fleet_preview,
            fleet::fleet_approve,
            fleet::fleet_remove,
            fleet::fleet_recover
        ])
        .build(tauri::generate_context!())
        .expect("error while building Flight Insights");
    app.run(|app, event| {
        if let RunEvent::Exit = event {
            let shell = app.state::<Shell>();
            shell.exiting.store(true, Ordering::SeqCst);
            // Wait out a start in progress, so the engine it starts is stopped too.
            let _starting = shell.starting.lock().expect("engine start");
            let engine = shell.engine.lock().expect("engine").take();
            if let Some(engine) = engine {
                engine.stop();
            }
        }
    });
}
