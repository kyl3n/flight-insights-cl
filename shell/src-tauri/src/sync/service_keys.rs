//! The fleet's service keys on the drive (spec: Security, "Service keys, with no server
//! to run"). An admin enters the Claude API key and the Open-Meteo key once; they travel
//! to every enrolled laptop as an object like any other, encrypted with the fleet key
//! and signed, and land in each laptop's OS keychain. They never reach the engine or
//! the interface.
//!
//! ```text
//! keys/<laptop-id>/<sequence>-<object-key>.fio
//! ```
//!
//! The `.fio` format is the devices' own. The sequence is the writer's clock in whole
//! seconds, always past the newest one seen, so the newest set wins. A set counts only
//! when it is written with the current fleet key by a laptop still in the fleet:
//! removing a laptop rotates the fleet key, which retires every set written before, and
//! the app asks for new keys, because the removed laptop held copies.
//!
//! A set written as `fi.keys/2` holds both keys; one written as `fi.keys/1`, by an app
//! from before weather, holds only the Claude key. Each key comes from the newest set
//! that holds it, so an older app's set changes the Claude key and leaves the
//! Open-Meteo key as it was. An older app reads only `fi.keys/1` sets.

use std::fs::{self, File};
use std::io::{self, BufWriter, Cursor};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

use crate::identity::LaptopIdentity;
use crate::sync::drive::Drive;
use crate::sync::keyring::{Keyring, Registry};
use crate::sync::object::{self, Keys, Name, Refusal};

pub const FOLDER: &str = "keys";
const FORMAT: &str = "fi.keys/2";
const FORMAT_CLAUDE_ONLY: &str = "fi.keys/1";

/// One set of service keys. Absent means "none": a set without a key removes it from
/// every laptop.
#[derive(Serialize, Deserialize)]
struct Plain {
    format: String,
    claude: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    open_meteo: Option<String>,
}

pub struct ServiceKeys {
    pub claude: Option<Zeroizing<String>>,
    /// None when no set that counts holds the Open-Meteo key yet (only older apps'
    /// sets): the key held here stays as it is. Some(None): removed.
    pub open_meteo: Option<Option<Zeroizing<String>>>,
    /// Where the newest set lies, to tell a new one from the one already applied.
    pub path: String,
}

impl std::fmt::Debug for ServiceKeys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let shown = |k: Option<&Zeroizing<String>>| k.map_or("none", |_| "set");
        f.debug_struct("ServiceKeys")
            .field("claude", &shown(self.claude.as_ref()))
            .field(
                "open_meteo",
                &self.open_meteo.as_ref().map(|k| shown(k.as_ref())),
            )
            .field("path", &self.path)
            .finish()
    }
}

/// Only the current fleet key opens a set, and only a laptop still in the fleet may
/// have signed it.
struct Current<'a> {
    registry: &'a Registry,
    keyring: &'a Keyring,
    object_keys: crate::sync::sodium::Key,
}

impl Keys for Current<'_> {
    fn check_signature(
        &self,
        laptop: &[u8; 16],
        sequence: u64,
        signed: &[u8],
        signature: &[u8; 64],
    ) -> Result<(), Refusal> {
        let member = self
            .registry
            .members
            .get(&hex::encode(laptop))
            .ok_or(Refusal("written by a laptop that is not enrolled"))?;
        if member.revoked_after.is_some() {
            return Err(Refusal("written by a laptop that was removed"));
        }
        self.registry.check(laptop, sequence, signed, signature)
    }
    fn fleet_key(&self, id: &[u8; 16]) -> Option<&[u8; 32]> {
        let current = self.keyring.current();
        (*id == current.id).then_some(current.key)
    }
    fn object_keys(&self) -> &[u8; 32] {
        &self.object_keys
    }
}

fn names(drive: &dyn Drive) -> io::Result<Vec<(Name, String)>> {
    let mut out: Vec<(Name, String)> = drive
        .list(&format!("{FOLDER}/"))?
        .into_iter()
        .filter_map(|e| Name::parse_in(FOLDER, &e.path).map(|n| (n, e.path)))
        .collect();
    // newest first; the same second on two laptops falls to the path, the same everywhere
    out.sort_by(|a, b| (b.0.sequence, &b.1).cmp(&(a.0.sequence, &a.1)));
    Ok(out)
}

/// The keys as the sets that hold give them: written with the current fleet key by a
/// laptop still in the fleet, each key from the newest such set that holds it. Sets
/// that do not hold are passed over, never applied. `seen` is the newest set already
/// applied: when it is still the newest, nothing is downloaded.
pub fn latest(
    drive: &dyn Drive,
    staging: &Path,
    registry: &Registry,
    keyring: &Keyring,
    seen: Option<&str>,
) -> Result<Option<ServiceKeys>, String> {
    let keys = Current {
        registry,
        keyring,
        object_keys: keyring.object_keys(),
    };
    fs::create_dir_all(staging).map_err(|e| e.to_string())?;
    let mut found: Option<ServiceKeys> = None;
    for (name, path) in names(drive).map_err(|e| e.to_string())? {
        if found.is_none() && seen == Some(path.as_str()) {
            return Ok(None);
        }
        let Some(plain) = open(drive, staging, &keys, &name, &path)? else {
            continue; // an old fleet key, a removed laptop, or tampering: never applied
        };
        let both = plain.format == FORMAT;
        let set = found.get_or_insert_with(|| ServiceKeys {
            claude: plain.claude.clone().map(Zeroizing::new),
            open_meteo: None,
            path,
        });
        if both {
            set.open_meteo = Some(plain.open_meteo.map(Zeroizing::new));
            break;
        }
    }
    Ok(found)
}

/// A set's contents, or None when it does not hold.
fn open(
    drive: &dyn Drive,
    staging: &Path,
    keys: &Current<'_>,
    name: &Name,
    path: &str,
) -> Result<Option<Plain>, String> {
    let local = staging.join(format!("keys-{}.fio", name.sequence));
    drive.download(path, &local).map_err(|e| e.to_string())?;
    let mut out = Zeroizing::new(Vec::new());
    let read = object::read(&local, name, keys, &mut *out);
    let _ = fs::remove_file(&local);
    if read.is_err() {
        return Ok(None);
    }
    Ok(serde_json::from_slice::<Plain>(&out)
        .ok()
        .filter(|p| p.format == FORMAT || p.format == FORMAT_CLAUDE_ONLY))
}

/// Writes a new set, with both keys as this laptop holds them, for every laptop in the
/// fleet. Returns where it lies.
pub fn publish(
    drive: &dyn Drive,
    staging: &Path,
    identity: &LaptopIdentity,
    keyring: &Keyring,
    claude: Option<&str>,
    open_meteo: Option<&str>,
) -> Result<String, String> {
    let plain = Plain {
        format: FORMAT.into(),
        claude: claude.map(str::to_owned),
        open_meteo: open_meteo.map(str::to_owned),
    };
    write_set(drive, staging, identity, keyring, &plain)
}

fn write_set(
    drive: &dyn Drive,
    staging: &Path,
    identity: &LaptopIdentity,
    keyring: &Keyring,
    plain: &Plain,
) -> Result<String, String> {
    let err = |e: io::Error| e.to_string();
    let newest = names(drive)
        .map_err(err)?
        .first()
        .map_or(0, |n| n.0.sequence);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let sequence = now.max(newest + 1);
    let plain = Zeroizing::new(serde_json::to_vec(plain).map_err(|e| e.to_string())?);
    let mut laptop = [0u8; 16];
    hex::decode_to_slice(identity.laptop_id_hex(), &mut laptop).map_err(|e| e.to_string())?;
    fs::create_dir_all(staging).map_err(err)?;
    let local = staging.join(format!("keys-out-{sequence}.fio"));
    let name = {
        let mut file = BufWriter::new(File::create(&local).map_err(err)?);
        object::write(
            &mut file,
            &mut Cursor::new(plain.as_slice()),
            laptop,
            sequence,
            &keyring.current(),
            &keyring.object_keys(),
            &|m| identity.sign(m),
        )
        .map_err(err)?
    };
    let path = name.path_in(FOLDER);
    let uploaded = drive.upload(&path, &local);
    let sent = fs::read(&local).map(|b| crate::sync::sodium::sha256(&b));
    let _ = fs::remove_file(&local);
    if uploaded.map_err(err)? != sent.map_err(err)? {
        return Err("the drive's copy of the service keys does not match".into());
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;
    use crate::sync::app;
    use crate::sync::drive::FolderDrive;
    use crate::sync::run::read_registry;
    use crate::sync::sodium;

    fn temp(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "fi-keys-{name}-{}",
            hex::encode(sodium::random::<6>().as_ref())
        ))
    }

    fn laptop() -> LaptopIdentity {
        LaptopIdentity::load_or_create(&MemoryStore::default())
            .unwrap()
            .0
    }

    const KEY: &str = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const NEW_KEY: &str = "sk-ant-api03-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    #[test]
    fn the_claude_key_reaches_every_laptop_and_a_removal_retires_it() {
        let root = temp("drive");
        let drive = FolderDrive::new(&root).unwrap();
        let staging = temp("staging");
        let (a, b, c) = (laptop(), laptop(), laptop());
        let made = app::create(&drive, &staging, &a, "Bench").unwrap();
        let fp = made.fingerprint;
        for (who, name) in [(&b, "Field"), (&c, "Spare")] {
            let (code, _) = app::code(who, name);
            app::approve(&drive, &staging, &fp, &made.keyring, &a, &code).unwrap();
        }
        let registry = read_registry(&drive, &staging, Some(&fp)).unwrap();
        let keys_b = app::join(&drive, &staging, &b, &fp).unwrap();
        let keys_c = app::join(&drive, &staging, &c, &fp).unwrap();
        assert!(
            latest(&drive, &staging, &registry, &keys_b, None)
                .unwrap()
                .is_none()
        );

        // A enters the key once; B and C receive it
        let at = publish(&drive, &staging, &a, &made.keyring, Some(KEY), None).unwrap();
        let got = latest(&drive, &staging, &registry, &keys_b, None)
            .unwrap()
            .unwrap();
        assert_eq!(got.claude.as_deref().map(|s| s.as_str()), Some(KEY));
        assert_eq!(got.path, at);
        assert!(
            !format!("{got:?}").contains("sk-ant"),
            "never in a log line"
        );
        // already applied: nothing more to do
        assert!(
            latest(&drive, &staging, &registry, &keys_b, Some(&at))
                .unwrap()
                .is_none()
        );
        // nothing on the drive holds the key in the clear
        for e in drive.list("keys/").unwrap() {
            let bytes = fs::read(root.join(&e.path)).unwrap();
            assert!(!bytes.windows(KEY.len()).any(|w| w == KEY.as_bytes()));
        }

        // A removes C: the fleet key rotates, and the old set no longer holds
        let rotated = app::remove(
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
        let keys_b = Keyring::from_registry(&registry, &b).unwrap();
        assert!(
            latest(&drive, &staging, &registry, &keys_b, None)
                .unwrap()
                .is_none(),
            "a removal asks for new keys"
        );
        // C still holds the old fleet key, but what it writes now is never applied
        publish(&drive, &staging, &c, &keys_c, Some("sk-ant-planted"), None).unwrap();
        assert!(
            latest(&drive, &staging, &registry, &keys_b, None)
                .unwrap()
                .is_none()
        );
        // the new key, under the new fleet key, reaches B
        let at = publish(&drive, &staging, &a, &rotated, Some(NEW_KEY), None).unwrap();
        let got = latest(&drive, &staging, &registry, &keys_b, None)
            .unwrap()
            .unwrap();
        assert_eq!(got.claude.as_deref().map(|s| s.as_str()), Some(NEW_KEY));
        assert_eq!(got.path, at);

        // removing the key is a set without it, and the newest set wins
        publish(&drive, &staging, &b, &keys_b, None, None).unwrap();
        let got = latest(&drive, &staging, &registry, &rotated, None)
            .unwrap()
            .unwrap();
        assert!(got.claude.is_none());
    }

    const OM_KEY: &str = "om0123456789abcdef";

    #[test]
    fn each_key_comes_from_the_newest_set_that_holds_it() {
        let root = temp("drive");
        let drive = FolderDrive::new(&root).unwrap();
        let staging = temp("staging");
        let (a, b) = (laptop(), laptop());
        let made = app::create(&drive, &staging, &a, "Bench").unwrap();
        let (code_b, _) = app::code(&b, "Field");
        app::approve(
            &drive,
            &staging,
            &made.fingerprint,
            &made.keyring,
            &a,
            &code_b,
        )
        .unwrap();
        let registry = read_registry(&drive, &staging, Some(&made.fingerprint)).unwrap();
        let keys_b = app::join(&drive, &staging, &b, &made.fingerprint).unwrap();
        let get = |seen: Option<&str>| latest(&drive, &staging, &registry, &keys_b, seen).unwrap();

        // an older app's set holds only the Claude key: the Open-Meteo key is unknown
        let plain = |format: &str, claude: &str| Plain {
            format: format.into(),
            claude: Some(claude.into()),
            open_meteo: None,
        };
        write_set(
            &drive,
            &staging,
            &a,
            &made.keyring,
            &plain("fi.keys/1", KEY),
        )
        .unwrap();
        let got = get(None).unwrap();
        assert_eq!(got.claude.as_deref().map(|s| s.as_str()), Some(KEY));
        assert!(got.open_meteo.is_none());

        // both keys, entered once, reach the other laptop
        let at = publish(&drive, &staging, &a, &made.keyring, Some(KEY), Some(OM_KEY)).unwrap();
        let got = get(None).unwrap();
        assert_eq!(got.path, at);
        let om = got
            .open_meteo
            .as_ref()
            .and_then(|k| k.as_deref().map(|s| s.as_str()));
        assert_eq!(om, Some(OM_KEY));
        assert!(!format!("{got:?}").contains(OM_KEY), "never in a log line");
        assert!(get(Some(&at)).is_none(), "already applied");

        // an older app changes the Claude key: the Open-Meteo key is kept
        let older = write_set(&drive, &staging, &b, &keys_b, &plain("fi.keys/1", NEW_KEY)).unwrap();
        let got = get(Some(&at)).unwrap();
        assert_eq!(got.path, older);
        assert_eq!(got.claude.as_deref().map(|s| s.as_str()), Some(NEW_KEY));
        let om = got
            .open_meteo
            .as_ref()
            .and_then(|k| k.as_deref().map(|s| s.as_str()));
        assert_eq!(om, Some(OM_KEY));

        // removing the Open-Meteo key is a set without it
        publish(&drive, &staging, &b, &keys_b, Some(NEW_KEY), None).unwrap();
        let got = get(None).unwrap();
        assert!(matches!(got.open_meteo, Some(None)));
        // an unknown format is never applied
        write_set(
            &drive,
            &staging,
            &a,
            &made.keyring,
            &plain("fi.keys/9", "sk-ant-x"),
        )
        .unwrap();
        let got = get(None).unwrap();
        assert_eq!(got.claude.as_deref().map(|s| s.as_str()), Some(NEW_KEY));
        // nothing on the drive holds either key in the clear
        for e in drive.list("keys/").unwrap() {
            let bytes = fs::read(root.join(&e.path)).unwrap();
            for k in [KEY, NEW_KEY, OM_KEY] {
                assert!(!bytes.windows(k.len()).any(|w| w == k.as_bytes()));
            }
        }
    }

    #[test]
    fn a_tampered_set_is_passed_over_for_the_one_before() {
        let root = temp("drive");
        let drive = FolderDrive::new(&root).unwrap();
        let staging = temp("staging");
        let a = laptop();
        let made = app::create(&drive, &staging, &a, "Bench").unwrap();
        let registry = read_registry(&drive, &staging, Some(&made.fingerprint)).unwrap();
        publish(&drive, &staging, &a, &made.keyring, Some(KEY), None).unwrap();
        let second = publish(&drive, &staging, &a, &made.keyring, Some(NEW_KEY), None).unwrap();
        let file = root.join(&second);
        let mut bytes = fs::read(&file).unwrap();
        let mid = bytes.len() / 2;
        bytes[mid] ^= 1;
        // the drive never overwrites; the test reaches past it
        let mut perms = fs::metadata(&file).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        perms.set_readonly(false);
        fs::set_permissions(&file, perms).unwrap();
        fs::write(&file, bytes).unwrap();
        let got = latest(&drive, &staging, &registry, &made.keyring, None)
            .unwrap()
            .unwrap();
        assert_eq!(got.claude.as_deref().map(|s| s.as_str()), Some(KEY));
    }
}
