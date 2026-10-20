//! Flight Insights desktop shell: windows, the engine sidecar, the OS keychain,
//! sync and outbound requests (see docs/SPEC.md, "System architecture").
//!
//! At start-up the shell reads (or on first start creates) this laptop's
//! identity in the OS keychain, starts the engine, and unlocks the engine's
//! encrypted store with the keys. The UI talks to the engine only through
//! `engine_call`, limited to `engine::UI_METHODS`. The shell makes no network
//! requests yet.

pub mod engine;
pub mod identity;
pub mod rpc;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde::Serialize;
use serde_json::Value;
use tauri::{AppHandle, Emitter, Manager, RunEvent};

use engine::{CALL_TIMEOUT, Engine, EngineCommand, UI_METHODS};
use identity::{LaptopIdentity, OsKeychain};

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
}

struct Shell {
    identity: Mutex<Option<Arc<LaptopIdentity>>>,
    engine: Mutex<Option<Arc<Engine>>>,
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
        engine
            .unlock(&identity, &self.data_dir)
            .map_err(|e| format!("the engine could not open the local store: {e}"))?;
        let engine = Arc::new(engine);
        *self.engine.lock().expect("engine") = Some(engine.clone());
        let mut s = self.status.lock().expect("status");
        s.engine = EngineState::Ready;
        s.engine_version = hello["engine"].as_str().map(str::to_owned);
        s.error = None;
        Ok(engine)
    }

    fn running_engine(&self, app: &AppHandle) -> Result<Arc<Engine>, String> {
        let current = self.engine.lock().expect("engine").clone();
        match current {
            Some(e) if e.is_running() => Ok(e),
            _ => {
                // The engine stopped (crashed or was killed): start a fresh one.
                let _ = app.emit("engine-restarting", ());
                self.start_engine(app)
            }
        }
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
        engine
            .client
            .call(&method, params, CALL_TIMEOUT)
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

fn start_up(app: &AppHandle) {
    let shell = app.state::<Shell>();
    let identifier = app.config().identifier.clone();
    let loaded =
        OsKeychain::new(&identifier).and_then(|keychain| LaptopIdentity::load_or_create(&keychain));
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
    match EngineCommand::resolve(&exe_dir) {
        Ok(cmd) => *shell.command.lock().expect("command") = Some(cmd),
        Err(e) => return shell.set_failed(e),
    }
    if let Err(e) = shell.start_engine(app) {
        shell.set_failed(e);
    }
    let _ = app.emit("shell-status", shell.status.lock().expect("status").clone());
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let data_dir = app.path().app_data_dir()?;
            let knowledge_dir = std::env::var_os("FI_KNOWLEDGE_DIR")
                .map(PathBuf::from)
                .or_else(|| app.path().resource_dir().ok().map(|d| d.join("knowledge")));
            app.manage(Shell {
                identity: Mutex::new(None),
                engine: Mutex::new(None),
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
        .invoke_handler(tauri::generate_handler![shell_status, engine_call])
        .build(tauri::generate_context!())
        .expect("error while building Flight Insights");
    app.run(|app, event| {
        if let RunEvent::Exit = event
            && let Some(engine) = app.state::<Shell>().engine.lock().expect("engine").take()
        {
            engine.stop();
        }
    });
}
