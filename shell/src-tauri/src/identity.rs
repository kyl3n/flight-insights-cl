//! The laptop's identity and keys (docs/SPEC.md, "Laptop identity" and "Encryption
//! at rest").
//!
//! On first start the shell makes a random 128-bit laptop ID, an Ed25519 signing
//! pair, an X25519 encryption pair, and the keys of the local store: database,
//! column cache, position columns, and the board-ID hashing key. Every private
//! byte lives in the OS keychain only, in a single entry, so the OS asks about it
//! once. Nothing here is ever written to a file or a log; `Debug` hides it.

use std::collections::HashMap;
use std::fmt;
use std::sync::{Arc, Mutex};

use ed25519_dalek::SigningKey;
use serde::{Deserialize, Serialize};
use x25519_dalek::{PublicKey, StaticSecret};
use zeroize::{Zeroize, Zeroizing};

pub const KEYCHAIN_ENTRY: &str = "laptop-identity";
const FORMAT: u32 = 1;

pub type Secret32 = Zeroizing<[u8; 32]>;

/// Where secrets are kept: the OS keychain in the app, memory in tests.
pub trait SecretStore: Send + Sync {
    fn get(&self, name: &str) -> Result<Option<Zeroizing<Vec<u8>>>, String>;
    fn set(&self, name: &str, secret: &[u8]) -> Result<(), String>;
    /// Removes the secret; removing one that is not there is not an error.
    fn delete(&self, name: &str) -> Result<(), String>;
}

/// The OS keychain: Windows Credential Manager, or the Secret Service on Linux
/// (GNOME Keyring, KWallet).
pub struct OsKeychain {
    service: String,
}

impl OsKeychain {
    pub fn new(service: &str) -> Result<Self, String> {
        #[cfg(target_os = "windows")]
        let store = windows_native_keyring_store::Store::new().map_err(|e| e.to_string())?;
        #[cfg(all(unix, not(target_os = "macos")))]
        let store = zbus_secret_service_keyring_store::Store::new().map_err(|e| e.to_string())?;
        #[cfg(target_os = "macos")]
        return Err("the macOS keychain is not wired up yet".into());
        #[cfg(not(target_os = "macos"))]
        keyring_core::set_default_store(store);
        #[cfg(not(target_os = "macos"))]
        Ok(OsKeychain {
            service: service.to_owned(),
        })
    }
}

impl SecretStore for OsKeychain {
    fn get(&self, name: &str) -> Result<Option<Zeroizing<Vec<u8>>>, String> {
        let entry = keyring_core::Entry::new(&self.service, name).map_err(|e| e.to_string())?;
        match entry.get_secret() {
            Ok(s) => Ok(Some(Zeroizing::new(s))),
            Err(keyring_core::Error::NoEntry) => Ok(None),
            Err(e) => Err(e.to_string()),
        }
    }

    fn set(&self, name: &str, secret: &[u8]) -> Result<(), String> {
        keyring_core::Entry::new(&self.service, name)
            .and_then(|e| e.set_secret(secret))
            .map_err(|e| e.to_string())
    }

    fn delete(&self, name: &str) -> Result<(), String> {
        let entry = keyring_core::Entry::new(&self.service, name).map_err(|e| e.to_string())?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring_core::Error::NoEntry) => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// Secrets in memory, for tests.
#[derive(Default, Clone)]
pub struct MemoryStore(Arc<Mutex<HashMap<String, Vec<u8>>>>);

impl SecretStore for MemoryStore {
    fn get(&self, name: &str) -> Result<Option<Zeroizing<Vec<u8>>>, String> {
        Ok(self
            .0
            .lock()
            .unwrap()
            .get(name)
            .cloned()
            .map(Zeroizing::new))
    }

    fn set(&self, name: &str, secret: &[u8]) -> Result<(), String> {
        self.0
            .lock()
            .unwrap()
            .insert(name.to_owned(), secret.to_vec());
        Ok(())
    }

    fn delete(&self, name: &str) -> Result<(), String> {
        self.0.lock().unwrap().remove(name);
        Ok(())
    }
}

/// The keys the engine needs to open the local store.
pub struct StoreKeys {
    pub database: Secret32,
    pub cache: Secret32,
    pub positions: Secret32,
    pub board_id: Secret32,
}

pub struct LaptopIdentity {
    laptop_id: [u8; 16],
    signing: SigningKey,
    encryption: StaticSecret,
    pub store_keys: StoreKeys,
}

impl fmt::Debug for LaptopIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LaptopIdentity")
            .field("laptop", &self.short_code())
            .field("keys", &"<hidden>")
            .finish()
    }
}

#[derive(Serialize, Deserialize)]
struct Stored {
    format: u32,
    laptop_id: String,
    signing: String,
    encryption: String,
    database: String,
    cache: String,
    positions: String,
    board_id: String,
}

impl Drop for Stored {
    fn drop(&mut self) {
        for s in [
            &mut self.signing,
            &mut self.encryption,
            &mut self.database,
            &mut self.cache,
            &mut self.positions,
            &mut self.board_id,
        ] {
            s.zeroize();
        }
    }
}

fn random<const N: usize>() -> Result<Zeroizing<[u8; N]>, String> {
    let mut b = Zeroizing::new([0u8; N]);
    getrandom::fill(b.as_mut()).map_err(|e| format!("no secure randomness: {e}"))?;
    Ok(b)
}

fn decode32(text: &str, what: &str) -> Result<Secret32, String> {
    let mut b = Zeroizing::new([0u8; 32]);
    hex::decode_to_slice(text, b.as_mut())
        .map_err(|_| format!("the keychain's {what} is damaged"))?;
    Ok(b)
}

impl LaptopIdentity {
    /// Read the identity from the keychain, or make and store a new one on first
    /// start. Returns whether it was new. A damaged entry is an error, never
    /// silently replaced: new keys could not open the data the old ones protect.
    pub fn load_or_create(store: &dyn SecretStore) -> Result<(Self, bool), String> {
        if let Some(blob) = store.get(KEYCHAIN_ENTRY)? {
            return Ok((Self::from_blob(&blob)?, false));
        }
        let identity = Self::generate()?;
        store.set(KEYCHAIN_ENTRY, &identity.to_blob()?)?;
        // Read it back, so a keychain that drops writes is caught now, not after
        // data has been encrypted with keys that are gone.
        let check = store
            .get(KEYCHAIN_ENTRY)?
            .ok_or("the keychain did not keep the laptop identity")?;
        if Self::from_blob(&check)?.laptop_id != identity.laptop_id {
            return Err("the keychain returned a different laptop identity".into());
        }
        Ok((identity, true))
    }

    fn generate() -> Result<Self, String> {
        let id = random::<16>()?;
        let signing = random::<32>()?;
        let encryption = random::<32>()?;
        Ok(LaptopIdentity {
            laptop_id: *id,
            signing: SigningKey::from_bytes(&signing),
            encryption: StaticSecret::from(*encryption),
            store_keys: StoreKeys {
                database: random()?,
                cache: random()?,
                positions: random()?,
                board_id: random()?,
            },
        })
    }

    fn to_blob(&self) -> Result<Zeroizing<Vec<u8>>, String> {
        let k = &self.store_keys;
        let stored = Stored {
            format: FORMAT,
            laptop_id: hex::encode(self.laptop_id),
            signing: hex::encode(self.signing.to_bytes()),
            encryption: hex::encode(self.encryption.to_bytes()),
            database: hex::encode(*k.database),
            cache: hex::encode(*k.cache),
            positions: hex::encode(*k.positions),
            board_id: hex::encode(*k.board_id),
        };
        serde_json::to_vec(&stored)
            .map(Zeroizing::new)
            .map_err(|e| e.to_string())
    }

    fn from_blob(blob: &[u8]) -> Result<Self, String> {
        let s: Stored = serde_json::from_slice(blob)
            .map_err(|_| "the keychain's laptop identity is damaged".to_string())?;
        if s.format != FORMAT {
            return Err(format!("unknown laptop identity format {}", s.format));
        }
        let mut id = [0u8; 16];
        hex::decode_to_slice(&s.laptop_id, &mut id)
            .map_err(|_| "the keychain's laptop ID is damaged".to_string())?;
        let signing = decode32(&s.signing, "signing key")?;
        let encryption = decode32(&s.encryption, "encryption key")?;
        Ok(LaptopIdentity {
            laptop_id: id,
            signing: SigningKey::from_bytes(&signing),
            encryption: StaticSecret::from(*encryption),
            store_keys: StoreKeys {
                database: decode32(&s.database, "database key")?,
                cache: decode32(&s.cache, "cache key")?,
                positions: decode32(&s.positions, "positions key")?,
                board_id: decode32(&s.board_id, "board-ID key")?,
            },
        })
    }

    pub fn laptop_id_hex(&self) -> String {
        hex::encode(self.laptop_id)
    }

    /// The short form people see, such as LT-3F9A2C.
    pub fn short_code(&self) -> String {
        format!("LT-{}", hex::encode_upper(&self.laptop_id[..3]))
    }

    pub fn signing_public(&self) -> [u8; 32] {
        self.signing.verifying_key().to_bytes()
    }

    pub fn encryption_public(&self) -> [u8; 32] {
        PublicKey::from(&self.encryption).to_bytes()
    }

    /// Opens a sealed box addressed to this laptop's encryption key (a fleet key
    /// granted to it in the device registry).
    pub fn open_sealed(&self, sealed: &[u8]) -> Result<Zeroizing<Vec<u8>>, String> {
        let secret = Zeroizing::new(self.encryption.to_bytes());
        crate::sync::sodium::seal_open(&self.encryption_public(), &secret, sealed)
    }

    pub fn sign(&self, message: &[u8]) -> [u8; 64] {
        use ed25519_dalek::Signer;
        self.signing.sign(message).to_bytes()
    }

    /// The engine.unlock params, written straight into memory that is wiped on drop:
    /// each key is hex-encoded into a wiped buffer, and the text is built in one
    /// allocation sized up front, so growing it never leaves a copy behind.
    /// `host` is this computer's host name, which the engine keeps only as a
    /// keyed hash, to recognise the ground station it runs on.
    /// Once the laptop is in a fleet, `fleet_board` is the fleet's board-ID key: board
    /// IDs are hashed with it, and this laptop's own goes along as the legacy key, so
    /// boards hashed before still match.
    pub fn unlock_params(
        &self,
        data_dir: &str,
        host: Option<&str>,
        fleet_board: Option<&Secret32>,
    ) -> Result<Zeroizing<String>, String> {
        let k = &self.store_keys;
        let dir = serde_json::to_string(data_dir).map_err(|e| e.to_string())?;
        let host = serde_json::to_string(&host).map_err(|e| e.to_string())?;
        let mut keys = vec![
            ("database", &k.database),
            ("cache", &k.cache),
            ("positions", &k.positions),
        ];
        match fleet_board {
            Some(fleet) => keys.extend([("board_id", fleet), ("legacy_board_id", &k.board_id)]),
            None => keys.push(("board_id", &k.board_id)),
        }
        // The fixed text and the laptop ID take under 128 bytes; each key under 80.
        let mut out = Zeroizing::new(String::with_capacity(
            128 + dir.len() + host.len() + 80 * keys.len(),
        ));
        let capacity = out.capacity();
        out.push_str(r#"{"laptop_id":""#);
        out.push_str(&self.laptop_id_hex());
        out.push_str(r#"","data_dir":"#);
        out.push_str(&dir);
        out.push_str(r#","host":"#);
        out.push_str(&host);
        out.push_str(r#","keys":{"#);
        let mut hex = Zeroizing::new([0u8; 64]);
        for (i, (name, key)) in keys.into_iter().enumerate() {
            hex::encode_to_slice(**key, hex.as_mut()).map_err(|e| e.to_string())?;
            if i > 0 {
                out.push(',');
            }
            out.push('"');
            out.push_str(name);
            out.push_str(r#"":""#);
            out.push_str(std::str::from_utf8(hex.as_ref()).expect("hex is ASCII"));
            out.push('"');
        }
        out.push_str("}}");
        debug_assert_eq!(
            out.capacity(),
            capacity,
            "the unlock params outgrew their buffer"
        );
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_start_creates_and_later_starts_read_the_same_identity() {
        let store = MemoryStore::default();
        let (first, created) = LaptopIdentity::load_or_create(&store).unwrap();
        assert!(created);
        let (again, created) = LaptopIdentity::load_or_create(&store).unwrap();
        assert!(!created);
        assert_eq!(first.laptop_id_hex(), again.laptop_id_hex());
        assert_eq!(first.signing_public(), again.signing_public());
        assert_eq!(first.encryption_public(), again.encryption_public());
        assert_eq!(*first.store_keys.database, *again.store_keys.database);
        assert_eq!(*first.store_keys.board_id, *again.store_keys.board_id);
    }

    #[test]
    fn keys_are_distinct_and_the_short_code_matches_the_id() {
        let (id, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        let k = &id.store_keys;
        let all = [*k.database, *k.cache, *k.positions, *k.board_id];
        for (i, a) in all.iter().enumerate() {
            for b in &all[i + 1..] {
                assert_ne!(a, b);
            }
        }
        assert_eq!(id.laptop_id_hex().len(), 32);
        assert_eq!(
            id.short_code(),
            format!("LT-{}", id.laptop_id_hex()[..6].to_uppercase())
        );
        let other = LaptopIdentity::load_or_create(&MemoryStore::default())
            .unwrap()
            .0;
        assert_ne!(id.laptop_id_hex(), other.laptop_id_hex());
    }

    #[test]
    fn signatures_verify_with_the_public_key() {
        use ed25519_dalek::{Signature, Verifier, VerifyingKey};
        let (id, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        let sig = Signature::from_bytes(&id.sign(b"object"));
        let public = VerifyingKey::from_bytes(&id.signing_public()).unwrap();
        assert!(public.verify(b"object", &sig).is_ok());
        assert!(public.verify(b"other", &sig).is_err());
    }

    #[test]
    fn a_damaged_entry_is_an_error_not_a_new_identity() {
        let store = MemoryStore::default();
        store.set(KEYCHAIN_ENTRY, b"{not json").unwrap();
        assert!(LaptopIdentity::load_or_create(&store).is_err());
        assert_eq!(&**store.get(KEYCHAIN_ENTRY).unwrap().unwrap(), b"{not json");
    }

    #[test]
    fn secrets_never_show_in_debug_output() {
        let (id, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        let shown = format!("{id:?}");
        assert!(shown.contains(&id.short_code()));
        for key in [
            &id.store_keys.database,
            &id.store_keys.cache,
            &id.store_keys.board_id,
        ] {
            assert!(!shown.contains(&hex::encode(**key)));
        }
    }

    #[test]
    fn unlock_params_are_valid_json_with_every_key() {
        let (id, _) = LaptopIdentity::load_or_create(&MemoryStore::default()).unwrap();
        let text = id
            .unlock_params("C:\\Users\\pilot \"x\"\\data", Some("GCS \"1\""), None)
            .unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["laptop_id"], id.laptop_id_hex());
        assert_eq!(v["host"], "GCS \"1\"");
        let none: serde_json::Value =
            serde_json::from_str(&id.unlock_params("d", None, None).unwrap()).unwrap();
        assert!(none["host"].is_null());
        assert_eq!(v["data_dir"], "C:\\Users\\pilot \"x\"\\data");
        for k in ["database", "cache", "positions", "board_id"] {
            assert_eq!(v["keys"][k].as_str().unwrap().len(), 64);
        }
        assert_eq!(v["keys"]["database"], hex::encode(*id.store_keys.database));
        assert_eq!(v["keys"]["board_id"], hex::encode(*id.store_keys.board_id));
        // a long path still fits the buffer sized up front (checked in debug builds)
        let long = "é\"\\".repeat(500);
        let v: serde_json::Value =
            serde_json::from_str(&id.unlock_params(&long, Some(&long), None).unwrap()).unwrap();
        assert_eq!(v["data_dir"], long.as_str());
    }
}
