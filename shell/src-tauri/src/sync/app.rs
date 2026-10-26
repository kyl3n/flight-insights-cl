//! Sync in the running app (spec: Shared storage and sync): the settings, the fleet
//! steps a person takes, and the worker that syncs every minute.
//!
//! - **Settings** (`sync-settings.json`, nothing secret): which shared drive, and the
//!   fleet's fingerprint, the hash of its first registry event, so a registry that is
//!   not this fleet's is refused. The Google refresh token and the fleet's keys live in
//!   the OS keychain only.
//! - **Fleet steps** work on any `Drive`, so the tests run them on a folder drive:
//!   create a fleet (the recovery words are returned once and kept nowhere), show this
//!   laptop's enrollment code, approve another laptop's code, join once approved,
//!   revoke a laptop, recover with the recovery words.
//! - **The worker** runs a round at start-up, every minute and when woken (after an
//!   import, or "Sync now"). Being offline is a state, not an error.

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Condvar, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::json;
use zeroize::Zeroizing;

use crate::identity::{LaptopIdentity, SecretStore};
use crate::sync::drive::Drive;
use crate::sync::keyring::{
    Device, Keyring, Registry, create_fleet, enroll, enrollment_code, read_enrollment_code,
    recover, revoke,
};
use crate::sync::run::{Calls, Round, Syncer, publish, publish_next, read_registry};

pub const REFRESH_TOKEN: &str = "google-refresh-token";
pub const ROUND_EVERY: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Settings {
    pub drive_id: Option<String>,
    pub drive_name: Option<String>,
    /// The fleet's fingerprint (hex), once this laptop is in a fleet.
    pub fleet: Option<String>,
}

fn settings_path(data_dir: &Path) -> PathBuf {
    data_dir.join("sync-settings.json")
}

pub fn load_settings(data_dir: &Path) -> Settings {
    std::fs::read_to_string(settings_path(data_dir))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

pub fn save_settings(data_dir: &Path, s: &Settings) -> Result<(), String> {
    std::fs::create_dir_all(data_dir).map_err(|e| e.to_string())?;
    let text = serde_json::to_string_pretty(s).map_err(|e| e.to_string())?;
    std::fs::write(settings_path(data_dir), text).map_err(|e| e.to_string())
}

impl Settings {
    pub fn fingerprint(&self) -> Option<[u8; 32]> {
        let mut out = [0u8; 32];
        hex::decode_to_slice(self.fleet.as_deref()?, &mut out).ok()?;
        Some(out)
    }
}

/// A fleet's fingerprint as people compare it: three groups of four.
pub fn show_fingerprint(fp: &[u8; 32]) -> String {
    let t = hex::encode_upper(&fp[..6]);
    format!("{}-{}-{}", &t[..4], &t[4..8], &t[8..])
}

pub fn refresh_token(store: &dyn SecretStore) -> Result<Option<Zeroizing<String>>, String> {
    Ok(store
        .get(REFRESH_TOKEN)?
        .and_then(|b| String::from_utf8(b.to_vec()).ok())
        .map(Zeroizing::new))
}

/// Opens an address in the system browser. Only the sign-in page is ever opened.
pub fn open_in_browser(url: &str) -> Result<(), String> {
    if !url.starts_with("https://") {
        return Err("only an https address is opened".into());
    }
    let started = if cfg!(target_os = "windows") {
        // The URL protocol handler takes the address as one argument; no shell parses it.
        Command::new("rundll32.exe")
            .args(["url.dll,FileProtocolHandler", url])
            .spawn()
    } else if cfg!(target_os = "macos") {
        Command::new("open").arg(url).spawn()
    } else {
        Command::new("xdg-open").arg(url).spawn()
    };
    started
        .map(|_| ())
        .map_err(|e| format!("the browser could not be opened: {e}"))
}

// --- fleet steps ----------------------------------------------------------------------

pub struct Created {
    pub keyring: Keyring,
    pub fingerprint: [u8; 32],
    /// Shown once, to be written down; never stored.
    pub words: Zeroizing<String>,
}

impl std::fmt::Debug for Created {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Created")
            .field("fingerprint", &show_fingerprint(&self.fingerprint))
            .finish_non_exhaustive()
    }
}

/// A new fleet on an empty shared drive, with this laptop its first member.
pub fn create(
    drive: &dyn Drive,
    staging: &Path,
    identity: &LaptopIdentity,
    name: &str,
) -> Result<Created, String> {
    if !drive
        .list("registry/")
        .map_err(|e| e.to_string())?
        .is_empty()
    {
        return Err(
            "this shared drive already has a fleet: join it from a laptop that is in it".into(),
        );
    }
    let (first, keyring, recovery) = create_fleet(identity, name)?;
    publish(drive, staging, &first, 0)?;
    let registry = read_registry(drive, staging, None)?;
    if registry.events[0].hash() != first.hash() {
        return Err(
            "another laptop set up a fleet on this drive at the same moment: join it instead"
                .into(),
        );
    }
    Ok(Created {
        keyring,
        fingerprint: registry.fingerprint,
        words: recovery.words(),
    })
}

/// This laptop's enrollment code, and the fingerprint the two people compare.
pub fn code(identity: &LaptopIdentity, name: &str) -> (String, String) {
    let device = Device::of(identity, name);
    (enrollment_code(&device), device.fingerprint())
}

/// What an enrollment code says, for the approving person to compare.
pub fn preview(code: &str) -> Result<Device, String> {
    read_enrollment_code(code)
}

pub fn approve(
    drive: &dyn Drive,
    staging: &Path,
    fingerprint: &[u8; 32],
    keyring: &Keyring,
    identity: &LaptopIdentity,
    code: &str,
) -> Result<Registry, String> {
    let device = read_enrollment_code(code)?;
    publish_next(drive, staging, fingerprint, &|r| {
        enroll(r, keyring, identity, device.clone())
    })
}

/// Once approved: the fleet's fingerprint for the person to compare with the approving
/// laptop's screen, or None if this laptop has not been approved yet.
pub fn find_fleet(
    drive: &dyn Drive,
    staging: &Path,
    identity: &LaptopIdentity,
) -> Result<Option<[u8; 32]>, String> {
    let registry = match read_registry(drive, staging, None) {
        Ok(r) => r,
        Err(_)
            if drive
                .list("registry/")
                .map_err(|e| e.to_string())?
                .is_empty() =>
        {
            return Ok(None);
        }
        Err(e) => return Err(e),
    };
    Ok(registry
        .members
        .contains_key(&identity.laptop_id_hex())
        .then_some(registry.fingerprint))
}

pub fn join(
    drive: &dyn Drive,
    staging: &Path,
    identity: &LaptopIdentity,
    fingerprint: &[u8; 32],
) -> Result<Keyring, String> {
    let registry = read_registry(drive, staging, Some(fingerprint))?;
    Keyring::from_registry(&registry, identity)
}

pub fn remove(
    drive: &dyn Drive,
    staging: &Path,
    fingerprint: &[u8; 32],
    keyring: &Keyring,
    identity: &LaptopIdentity,
    laptop: &str,
    last_sequence: u64,
) -> Result<Keyring, String> {
    if laptop == identity.laptop_id_hex() {
        return Err("a laptop cannot remove itself: remove it from another laptop".into());
    }
    let rotated: RefCell<Option<Keyring>> = RefCell::new(None);
    publish_next(drive, staging, fingerprint, &|r| {
        let (event, keys) = revoke(r, keyring, identity, laptop, last_sequence)?;
        *rotated.borrow_mut() = Some(keys);
        Ok(event)
    })?;
    rotated
        .into_inner()
        .ok_or_else(|| "the new keys were not made".into())
}

pub fn recover_with(
    drive: &dyn Drive,
    staging: &Path,
    identity: &LaptopIdentity,
    words: &str,
    name: &str,
) -> Result<(Keyring, [u8; 32]), String> {
    let registry = read_registry(drive, staging, None)?;
    let got: RefCell<Option<Keyring>> = RefCell::new(None);
    let after = publish_next(drive, staging, &registry.fingerprint, &|r| {
        let (event, keys) = recover(r, words, identity, name)?;
        *got.borrow_mut() = Some(keys);
        Ok(event)
    })?;
    let keys = got.into_inner().ok_or("the keys were not recovered")?;
    Ok((keys, after.fingerprint))
}

/// The members as the interface shows them.
pub fn members(registry: &Registry) -> Vec<serde_json::Value> {
    registry
        .members
        .values()
        .map(|m| {
            json!({ "id": m.device.id, "name": m.device.name, "fingerprint": m.device.fingerprint(),
                    "revoked": m.revoked_after.is_some() })
        })
        .collect()
}

// --- the worker -------------------------------------------------------------------------

/// What the header and the sync panel show.
#[derive(Debug, Clone, Default, Serialize)]
pub struct State {
    /// off (not set up), idle, syncing, offline, sign_in (Google wants a new sign-in),
    /// revoked (this laptop was removed from the fleet), error
    pub state: String,
    pub why: Option<String>,
    pub last_sync_ms: Option<u64>,
    pub uploaded: usize,
    pub applied: usize,
    pub waiting: usize,
    pub quarantined: usize,
    pub sources_moved: usize,
    pub gaps: Vec<(String, u64)>,
}

#[derive(Default)]
pub struct Worker {
    pub state: Mutex<State>,
    cursor: Mutex<Option<String>>,
    woken: Mutex<bool>,
    wake: Condvar,
    /// The service keys set already applied here, by its path on the drive.
    pub keys_seen: Mutex<Option<String>>,
    /// A key entered or removed here: this laptop's keys wait to go to the fleet.
    pub keys_out: Mutex<bool>,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

impl Worker {
    pub fn wake(&self) {
        *self.woken.lock().expect("wake") = true;
        self.wake.notify_all();
    }

    /// Waits for the next round: a minute, or until woken.
    pub fn wait(&self) {
        let woken = self.woken.lock().expect("wake");
        let (mut woken, _) = self
            .wake
            .wait_timeout_while(woken, ROUND_EVERY, |w| !*w)
            .expect("wake");
        *woken = false;
    }

    /// Shares this laptop's service keys, as its keychain holds them, with the fleet at
    /// the next round: after a key is entered or removed here.
    pub fn share_keys(&self) {
        *self.keys_out.lock().expect("keys out") = true;
        self.wake();
    }

    pub fn set(&self, state: &str, why: Option<String>) {
        let mut s = self.state.lock().expect("sync state");
        s.state = state.into();
        s.why = why;
    }

    /// This laptop's keys as the registry now grants them (after a rotation, the new
    /// fleet key too), or None when this laptop was removed from the fleet.
    pub fn keys_for(
        registry: &Registry,
        identity: &LaptopIdentity,
    ) -> Result<Option<Keyring>, String> {
        match registry.members.get(&identity.laptop_id_hex()) {
            Some(m) if m.revoked_after.is_none() => {
                Keyring::from_registry(registry, identity).map(Some)
            }
            _ => Ok(None),
        }
    }

    /// One round, recorded for the interface.
    pub fn round(
        &self,
        engine: &dyn Calls,
        drive: &dyn Drive,
        identity: &LaptopIdentity,
        registry: &Registry,
        keyring: &Keyring,
        staging: &Path,
    ) -> Result<Round, String> {
        let force = self.state.lock().expect("sync state").waiting > 0;
        self.set("syncing", None);
        let mut cursor = self.cursor.lock().expect("cursor");
        let syncer = Syncer {
            engine,
            drive,
            identity,
            registry,
            keyring,
            staging: staging.to_path_buf(),
        };
        let round = syncer.round_watching(&mut cursor, force)?;
        let mut s = self.state.lock().expect("sync state");
        *s = State {
            state: "idle".into(),
            why: None,
            last_sync_ms: Some(now_ms()),
            uploaded: round.uploaded,
            applied: round.applied,
            waiting: round.waiting,
            quarantined: round.quarantined,
            sources_moved: round.sources_moved,
            gaps: round.gaps.clone(),
        };
        Ok(round)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;
    use crate::sync::drive::FolderDrive;
    use crate::sync::sodium;

    fn temp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "fi-app-{name}-{}",
            hex::encode(sodium::random::<6>().as_ref())
        ))
    }

    fn laptop() -> LaptopIdentity {
        LaptopIdentity::load_or_create(&MemoryStore::default())
            .unwrap()
            .0
    }

    #[test]
    fn a_fleet_is_made_joined_shrunk_and_recovered_through_a_drive() {
        let root = temp("drive");
        let drive = FolderDrive::new(&root).unwrap();
        let staging = temp("staging");
        let (a, b, c) = (laptop(), laptop(), laptop());
        // A sets up the fleet; the words are given once
        let made = create(&drive, &staging, &a, "Bench").unwrap();
        assert_eq!(made.words.split(' ').count(), 24);
        assert!(
            create(&drive, &staging, &b, "Again")
                .unwrap_err()
                .contains("already has a fleet")
        );
        // B shows its code; A approves it after comparing fingerprints
        let (code_b, shown) = code(&b, "Field");
        assert_eq!(preview(&code_b).unwrap().fingerprint(), shown);
        assert_eq!(
            find_fleet(&drive, &staging, &b).unwrap(),
            None,
            "not approved yet"
        );
        approve(
            &drive,
            &staging,
            &made.fingerprint,
            &made.keyring,
            &a,
            &code_b,
        )
        .unwrap();
        let fp = find_fleet(&drive, &staging, &b).unwrap().unwrap();
        assert_eq!(fp, made.fingerprint);
        let keys_b = join(&drive, &staging, &b, &fp).unwrap();
        assert_eq!(*keys_b.board_key(), *made.keyring.board_key());
        // a stranger's fingerprint is refused
        assert!(join(&drive, &staging, &b, &[1u8; 32]).is_err());
        // C joins, then A removes it; B's keys follow the rotation
        approve(
            &drive,
            &staging,
            &fp,
            &made.keyring,
            &a,
            &code(&c, "Spare").0,
        )
        .unwrap();
        assert!(
            remove(
                &drive,
                &staging,
                &fp,
                &made.keyring,
                &a,
                &a.laptop_id_hex(),
                0
            )
            .is_err()
        );
        let rotated = remove(
            &drive,
            &staging,
            &fp,
            &made.keyring,
            &a,
            &c.laptop_id_hex(),
            0,
        )
        .unwrap();
        let registry = read_registry(&drive, &staging, Some(&fp)).unwrap();
        let b_now = Worker::keys_for(&registry, &b).unwrap().unwrap();
        assert_eq!(b_now.current().id, rotated.current().id);
        let c_keys = join(&drive, &staging, &c, &fp).unwrap();
        assert!(
            c_keys.fleet_key(&rotated.current().id).is_none(),
            "C never gets the new key"
        );
        assert!(
            Worker::keys_for(&registry, &c).unwrap().is_none(),
            "C was removed"
        );
        let shown: Vec<bool> = members(&registry)
            .iter()
            .map(|m| m["revoked"].as_bool().unwrap())
            .collect();
        assert_eq!(shown.iter().filter(|r| **r).count(), 1);
        // a fresh laptop comes back in with the words alone
        let d = laptop();
        let (keys_d, fp_d) =
            recover_with(&drive, &staging, &d, &made.words, "Replacement").unwrap();
        assert_eq!((fp_d, keys_d.current().id), (fp, rotated.current().id));
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&staging);
    }

    #[test]
    fn settings_round_trip_and_hold_nothing_secret() {
        let dir = temp("settings");
        let s = Settings {
            drive_id: Some("d1".into()),
            drive_name: Some("Fleet".into()),
            fleet: Some("ab".repeat(32)),
        };
        save_settings(&dir, &s).unwrap();
        assert_eq!(load_settings(&dir), s);
        assert_eq!(s.fingerprint().unwrap()[0], 0xab);
        assert_eq!(show_fingerprint(&[0xab; 32]), "ABAB-ABAB-ABAB");
        let text = std::fs::read_to_string(settings_path(&dir)).unwrap();
        assert!(!text.contains("token") && !text.contains("key"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn only_https_addresses_are_opened() {
        assert!(open_in_browser("file:///etc/passwd").is_err());
        assert!(open_in_browser("javascript:alert(1)").is_err());
    }
}
