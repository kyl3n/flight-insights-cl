//! Sync and the fleet, as the interface reaches them (spec: Shared storage and sync):
//! signing in to Google, choosing the shared drive, setting up or joining a fleet,
//! approving and removing laptops, recovering with the recovery words, and the
//! worker's round every minute, which also carries the fleet's service keys. The
//! interface never sees a token or a key: only
//! whether there is one, fingerprints to compare, and the recovery words once.

use std::path::PathBuf;
use std::sync::Arc;

use serde_json::{Value, json};
use tauri::{AppHandle, Emitter, Manager};

use crate::Shell;
use crate::ai;
use crate::identity::{LaptopIdentity, SecretStore};
use crate::sync::app::{self, Settings, Worker, refresh_token, show_fingerprint};
use crate::sync::google::{GoogleDrive, shared_drives};
use crate::sync::keyring::{Keyring, Registry};
use crate::sync::oauth::{self, Endpoints};
use crate::sync::run::read_registry;
use crate::sync::service_keys;
use crate::weather;

const SIGN_IN_WAIT: std::time::Duration = std::time::Duration::from_secs(300);

struct Parts {
    keychain: Arc<dyn SecretStore>,
    identity: Arc<LaptopIdentity>,
    data_dir: PathBuf,
}

fn parts(shell: &Shell) -> Result<Parts, String> {
    Ok(Parts {
        keychain: shell
            .keychain
            .lock()
            .expect("keychain")
            .clone()
            .ok_or("the OS keychain is not available")?,
        identity: shell
            .identity
            .lock()
            .expect("identity")
            .clone()
            .ok_or("the laptop identity is not available")?,
        data_dir: shell.data_dir.clone(),
    })
}

fn staging(data_dir: &std::path::Path) -> PathBuf {
    data_dir.join("sync-staging")
}

fn registry_staging(data_dir: &std::path::Path) -> PathBuf {
    staging(data_dir).join("registry")
}

/// The shared drive this laptop uses, signed in.
fn google(shell: &Shell, p: &Parts) -> Result<Arc<GoogleDrive>, String> {
    let settings = app::load_settings(&p.data_dir);
    let drive_id = settings.drive_id.ok_or("choose the shared drive first")?;
    let mut cached = shell.drive.lock().expect("drive");
    if let Some((id, d)) = cached.as_ref()
        && *id == drive_id
    {
        return Ok(d.clone());
    }
    let client = oauth::built_in_client().ok_or("Google sign-in is not set up in this build")?;
    let refresh = refresh_token(p.keychain.as_ref())?.ok_or("sign in to Google first")?;
    let drive = Arc::new(
        GoogleDrive::new(Endpoints::default(), client, &drive_id, refresh)
            .map_err(|e| e.to_string())?,
    );
    *cached = Some((drive_id, drive.clone()));
    Ok(drive)
}

fn fleet_of(p: &Parts) -> Result<(Settings, [u8; 32], Keyring), String> {
    let settings = app::load_settings(&p.data_dir);
    let fp = settings
        .fingerprint()
        .ok_or("this laptop is not in a fleet yet")?;
    let keys = Keyring::load(p.keychain.as_ref())?.ok_or("this laptop's fleet keys are missing")?;
    Ok((settings, fp, keys))
}

/// Joining or leaving a fleet changes the board-ID key: the engine is started again
/// with it, and the hashes are made again (the WP18 re-key).
fn restart_engine(app: &AppHandle) -> Result<(), String> {
    let shell = app.state::<Shell>();
    let _starting = shell.starting.lock().expect("engine start");
    if let Some(engine) = shell.engine.lock().expect("engine").take() {
        engine.stop();
    }
    let started = shell.start_engine(app);
    let _ = app.emit("shell-status", shell.status.lock().expect("status").clone());
    started.map(|_| ())
}

fn joined(app: &AppHandle, p: &Parts, keys: &Keyring, fp: &[u8; 32]) -> Result<(), String> {
    keys.save(p.keychain.as_ref())?;
    let mut settings = app::load_settings(&p.data_dir);
    settings.fleet = Some(hex::encode(fp));
    app::save_settings(&p.data_dir, &settings)?;
    restart_engine(app)?;
    app.state::<Shell>().sync.wake();
    Ok(())
}

// --- the worker -------------------------------------------------------------------------

/// One round of the worker, and the state the interface shows.
pub fn round(app: &AppHandle) {
    let shell = app.state::<Shell>();
    let outcome = (|| -> Result<(), (String, String)> {
        let off = |why: &str| ("off".to_string(), why.to_string());
        let p = parts(&shell).map_err(|e| ("error".into(), e))?;
        let settings = app::load_settings(&p.data_dir);
        let fp = settings
            .fingerprint()
            .ok_or_else(|| off("set up or join a fleet"))?;
        if oauth::built_in_client().is_none() {
            return Err(off("Google sign-in is not set up in this build"));
        }
        if refresh_token(p.keychain.as_ref())
            .map_err(|e| ("error".into(), e))?
            .is_none()
        {
            return Err(off("sign in to Google"));
        }
        let drive = google(&shell, &p).map_err(|e| off(&e))?;
        let engine = shell.current_engine().ok_or_else(|| {
            (
                "offline".to_string(),
                "the engine is not running".to_string(),
            )
        })?;
        let failed = |e: String| {
            if e.contains("sign in again") {
                ("sign_in".to_string(), e)
            } else {
                ("offline".to_string(), e)
            }
        };
        let registry = read_registry(drive.as_ref(), &registry_staging(&p.data_dir), Some(&fp))
            .map_err(failed)?;
        let Some(keys) =
            Worker::keys_for(&registry, &p.identity).map_err(|e| ("error".into(), e))?
        else {
            return Err((
                "revoked".into(),
                "this laptop was removed from the fleet".into(),
            ));
        };
        let held = Keyring::load(p.keychain.as_ref()).map_err(|e| ("error".into(), e))?;
        if held
            .as_ref()
            .is_none_or(|k| k.current().id != keys.current().id)
        {
            keys.save(p.keychain.as_ref())
                .map_err(|e| ("error".into(), e))?; // a rotation
        }
        *shell.registry.lock().expect("registry") = Some(registry.clone());
        shell
            .sync
            .round(
                engine.as_ref(),
                drive.as_ref(),
                &p.identity,
                &registry,
                &keys,
                &staging(&p.data_dir),
            )
            .map_err(failed)?;
        service_keys(&shell, &p, drive.as_ref(), &registry, &keys).map_err(failed)?;
        Ok(())
    })();
    if let Err((state, why)) = outcome {
        shell.sync.set(&state, Some(why));
    }
    let _ = app.emit(
        "sync-status",
        shell.sync.state.lock().expect("sync state").clone(),
    );
}

/// The fleet's service keys: keys entered or removed here go out first, then the
/// newest sets on the drive come into the keychain. Neither reaches the engine or the
/// interface.
fn service_keys(
    shell: &Shell,
    p: &Parts,
    drive: &dyn crate::sync::drive::Drive,
    registry: &Registry,
    keys: &Keyring,
) -> Result<(), String> {
    let staging = staging(&p.data_dir);
    let out = std::mem::take(&mut *shell.sync.keys_out.lock().expect("keys out"));
    if out {
        let claude = ai::key(p.keychain.as_ref())?;
        let open_meteo = weather::key(p.keychain.as_ref())?;
        match service_keys::publish(
            drive,
            &staging,
            &p.identity,
            keys,
            claude.as_deref().map(String::as_str),
            open_meteo.as_deref().map(String::as_str),
        ) {
            Ok(path) => *shell.sync.keys_seen.lock().expect("keys seen") = Some(path),
            Err(e) => {
                // still to go: kept for the next round
                *shell.sync.keys_out.lock().expect("keys out") = true;
                return Err(e);
            }
        }
    }
    let seen = shell.sync.keys_seen.lock().expect("keys seen").clone();
    if let Some(set) = service_keys::latest(drive, &staging, registry, keys, seen.as_deref())? {
        match &set.claude {
            Some(k) => ai::set_key(p.keychain.as_ref(), k)?,
            None => p.keychain.delete(ai::KEY_NAME)?,
        }
        match &set.open_meteo {
            Some(Some(k)) => weather::set_key(p.keychain.as_ref(), k)?,
            Some(None) => p.keychain.delete(weather::KEY_NAME)?,
            None => {} // no set that counts speaks of it yet: kept as it is
        }
        shell
            .key_rejected
            .store(false, std::sync::atomic::Ordering::SeqCst);
        *shell.sync.keys_seen.lock().expect("keys seen") = Some(set.path);
    }
    Ok(())
}

/// Whether this laptop is in a fleet, so a key entered here is shared with it.
pub fn in_fleet(shell: &Shell) -> bool {
    app::load_settings(&shell.data_dir).fleet.is_some()
}

// --- commands ---------------------------------------------------------------------------

async fn blocking<T: Send + 'static>(
    app: AppHandle,
    f: impl FnOnce(&AppHandle) -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    tauri::async_runtime::spawn_blocking(move || f(&app))
        .await
        .map_err(|e| e.to_string())?
}

fn members_of(registry: Option<&Registry>, me: &str) -> Value {
    registry.map_or(json!([]), |r| {
        Value::Array(
            app::members(r)
                .into_iter()
                .map(|mut m| {
                    m["me"] = json!(m["id"] == me);
                    m
                })
                .collect(),
        )
    })
}

/// Everything the sync panel shows: no token, no key.
#[tauri::command]
pub async fn sync_status(app: AppHandle) -> Result<Value, String> {
    blocking(app, |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let settings = app::load_settings(&p.data_dir);
        let registry = shell.registry.lock().expect("registry").clone();
        let engine = shell
            .current_engine()
            .and_then(|e| crate::claude::Engine::call(e.as_ref(), "sync.status", json!({})).ok());
        Ok(json!({
            "client": oauth::built_in_client().is_some(),
            "signed_in": refresh_token(p.keychain.as_ref())?.is_some(),
            "drive": settings.drive_id.as_ref().map(|id| json!({ "id": id, "name": settings.drive_name })),
            "fleet": settings.fingerprint().map(|fp| json!({
                "fingerprint": show_fingerprint(&fp),
                "members": members_of(registry.as_ref(), &p.identity.laptop_id_hex()),
            })),
            "worker": shell.sync.state.lock().expect("sync state").clone(),
            "engine": engine,
        }))
    })
    .await
}

#[tauri::command]
pub async fn google_sign_in(app: AppHandle) -> Result<(), String> {
    blocking(app, |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let client =
            oauth::built_in_client().ok_or("Google sign-in is not set up in this build")?;
        let signed = oauth::sign_in(
            &Endpoints::default(),
            &client,
            &app::open_in_browser,
            SIGN_IN_WAIT,
        )
        .map_err(|e| e.to_string())?;
        p.keychain
            .set(app::REFRESH_TOKEN, signed.refresh.as_bytes())?;
        *shell.drive.lock().expect("drive") = None;
        shell.sync.wake();
        Ok(())
    })
    .await
}

#[tauri::command]
pub async fn google_sign_out(app: AppHandle) -> Result<(), String> {
    blocking(app, |app| {
        let shell = app.state::<Shell>();
        parts(&shell)?.keychain.delete(app::REFRESH_TOKEN)?;
        *shell.drive.lock().expect("drive") = None;
        shell.sync.set("off", Some("sign in to Google".into()));
        Ok(())
    })
    .await
}

#[tauri::command]
pub async fn google_drives(app: AppHandle) -> Result<Vec<Value>, String> {
    blocking(app, |app| {
        let p = parts(&app.state::<Shell>())?;
        let client =
            oauth::built_in_client().ok_or("Google sign-in is not set up in this build")?;
        let refresh = refresh_token(p.keychain.as_ref())?.ok_or("sign in to Google first")?;
        let drives =
            shared_drives(&Endpoints::default(), &client, &refresh).map_err(|e| e.to_string())?;
        Ok(drives
            .into_iter()
            .map(|(id, name)| json!({ "id": id, "name": name }))
            .collect())
    })
    .await
}

#[tauri::command]
pub async fn sync_choose_drive(app: AppHandle, id: String, name: String) -> Result<(), String> {
    blocking(app, move |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let mut settings = app::load_settings(&p.data_dir);
        if settings.fleet.is_some() && settings.drive_id.as_deref() != Some(id.as_str()) {
            return Err("this laptop's fleet is on another shared drive".into());
        }
        settings.drive_id = Some(id);
        settings.drive_name = Some(name.chars().take(200).collect());
        app::save_settings(&p.data_dir, &settings)?;
        *shell.drive.lock().expect("drive") = None;
        shell.sync.wake();
        Ok(())
    })
    .await
}

/// A new fleet on the chosen drive. The recovery words are returned this once.
#[tauri::command]
pub async fn fleet_create(app: AppHandle, name: String) -> Result<Value, String> {
    blocking(app, move |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        if app::load_settings(&p.data_dir).fleet.is_some() {
            return Err("this laptop is already in a fleet".into());
        }
        let drive = google(&shell, &p)?;
        let made = app::create(drive.as_ref(), &registry_staging(&p.data_dir), &p.identity, &name)?;
        joined(app, &p, &made.keyring, &made.fingerprint)?;
        Ok(json!({ "words": made.words.as_str(), "fingerprint": show_fingerprint(&made.fingerprint) }))
    })
    .await
}

/// This laptop's enrollment code, for an enrolled laptop to approve.
#[tauri::command]
pub async fn fleet_code(app: AppHandle, name: String) -> Result<Value, String> {
    blocking(app, move |app| {
        let p = parts(&app.state::<Shell>())?;
        let (code, fingerprint) = app::code(&p.identity, &name);
        Ok(json!({ "code": code, "fingerprint": fingerprint }))
    })
    .await
}

/// Whether this laptop has been approved: the fleet's fingerprint to compare.
#[tauri::command]
pub async fn fleet_find(app: AppHandle) -> Result<Option<String>, String> {
    blocking(app, |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let drive = google(&shell, &p)?;
        Ok(
            app::find_fleet(drive.as_ref(), &registry_staging(&p.data_dir), &p.identity)?
                .map(|fp| show_fingerprint(&fp)),
        )
    })
    .await
}

/// Joins the fleet whose fingerprint the person compared and confirmed.
#[tauri::command]
pub async fn fleet_join(app: AppHandle, fingerprint: String) -> Result<(), String> {
    blocking(app, move |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let drive = google(&shell, &p)?;
        let staging = registry_staging(&p.data_dir);
        let fp = app::find_fleet(drive.as_ref(), &staging, &p.identity)?
            .ok_or("this laptop has not been approved yet")?;
        if show_fingerprint(&fp) != fingerprint.trim().to_uppercase() {
            return Err("the fleet's fingerprint is not the one confirmed: do not join".into());
        }
        let keys = app::join(drive.as_ref(), &staging, &p.identity, &fp)?;
        joined(app, &p, &keys, &fp)
    })
    .await
}

/// What a code says, for the person approving to compare with the other screen.
#[tauri::command]
pub fn fleet_preview(code: String) -> Result<Value, String> {
    let device = app::preview(&code)?;
    Ok(json!({ "name": device.name, "fingerprint": device.fingerprint() }))
}

#[tauri::command]
pub async fn fleet_approve(app: AppHandle, code: String) -> Result<(), String> {
    blocking(app, move |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let (_, fp, keys) = fleet_of(&p)?;
        let drive = google(&shell, &p)?;
        let registry = app::approve(
            drive.as_ref(),
            &registry_staging(&p.data_dir),
            &fp,
            &keys,
            &p.identity,
            &code,
        )?;
        *shell.registry.lock().expect("registry") = Some(registry);
        Ok(())
    })
    .await
}

/// Removes a laptop and rotates the fleet key. The interface then asks for new service
/// keys: the removed laptop held copies.
#[tauri::command]
pub async fn fleet_remove(app: AppHandle, laptop: String) -> Result<(), String> {
    blocking(app, move |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        let (_, fp, keys) = fleet_of(&p)?;
        let drive = google(&shell, &p)?;
        let engine = shell.current_engine().ok_or("the engine is not running")?;
        // the newest of its objects this laptop has applied still counts
        let status = crate::claude::Engine::call(engine.as_ref(), "sync.status", json!({}))?;
        let last = status["laptops"]
            .as_array()
            .and_then(|ls| ls.iter().find(|l| l["laptop"] == laptop.as_str()))
            .and_then(|l| l["latest"].as_u64())
            .unwrap_or(0);
        let rotated = app::remove(
            drive.as_ref(),
            &registry_staging(&p.data_dir),
            &fp,
            &keys,
            &p.identity,
            &laptop,
            last,
        )?;
        rotated.save(p.keychain.as_ref())?;
        *shell.registry.lock().expect("registry") = Some(read_registry(
            drive.as_ref(),
            &registry_staging(&p.data_dir),
            Some(&fp),
        )?);
        shell.sync.wake();
        Ok(())
    })
    .await
}

/// A fresh laptop back in with the recovery words, when no enrolled laptop can approve it.
#[tauri::command]
pub async fn fleet_recover(app: AppHandle, words: String, name: String) -> Result<String, String> {
    let words = zeroize::Zeroizing::new(words);
    blocking(app, move |app| {
        let shell = app.state::<Shell>();
        let p = parts(&shell)?;
        if app::load_settings(&p.data_dir).fleet.is_some() {
            return Err("this laptop is already in a fleet".into());
        }
        let drive = google(&shell, &p)?;
        let (keys, fp) = app::recover_with(
            drive.as_ref(),
            &registry_staging(&p.data_dir),
            &p.identity,
            &words,
            &name,
        )?;
        joined(app, &p, &keys, &fp)?;
        Ok(show_fingerprint(&fp))
    })
    .await
}

#[tauri::command]
pub fn sync_now(shell: tauri::State<'_, Shell>) {
    shell.sync.wake();
}
