//! The fleet's keys and its device registry (spec: Shared storage and sync, "Laptop
//! identity", "Encryption", "Never losing data").
//!
//! - **Keys.** A fleet has a root key, which never changes and names things (object
//!   names, board-ID hashes), and fleet keys, which encrypt objects. Revoking a laptop
//!   adds a new fleet key; objects written afterwards use it, so the revoked laptop can
//!   read nothing new. Every laptop holds the root and every fleet key, and so does the
//!   recovery key.
//! - **Registry.** An append-only chain of signed events on the drive: the fleet's
//!   creation, each enrollment, each revocation (with the new fleet key), and each
//!   recovery. Each event names the hash of the one before it and is signed by a laptop
//!   enrolled and not revoked at that point, or, for a recovery, by the recovery key.
//!   A laptop that joins checks the chain from its first event, whose hash is the
//!   fleet's fingerprint.
//! - **Grants.** Keys reach a laptop sealed to its own encryption key, inside the event
//!   that enrolls it or rotates the keys; nobody else can open them.
//! - **Recovery key.** 32 random bytes shown once as 24 words (BIP-39 English). It opens
//!   a copy of every key kept in the registry, and signs the event that lets a fresh
//!   laptop in when no enrolled laptop is left.
//!
//! The keys this laptop holds live only in the OS keychain.

use std::collections::{BTreeMap, BTreeSet};
use std::time::{SystemTime, UNIX_EPOCH};

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde::{Deserialize, Serialize};
use zeroize::{Zeroize, Zeroizing};

use crate::identity::{LaptopIdentity, SecretStore};
use crate::sync::object::{self, Refusal};
use crate::sync::sodium::{self, Key};

pub const KEYCHAIN_ENTRY: &str = "fleet-keyring";
const SIGNED: &[u8] = b"fi.registry/1";
const WORDS: &str = include_str!("bip39-english.txt");

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

fn unhex<const N: usize>(text: &str) -> Result<[u8; N], String> {
    let mut out = [0u8; N];
    hex::decode_to_slice(text, &mut out)
        .map_err(|_| "a damaged value in the registry".to_string())?;
    Ok(out)
}

// --- the registry ---------------------------------------------------------------------

/// A laptop as the registry knows it: public keys only.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub signing: String,
    pub encryption: String,
}

impl Device {
    pub fn of(identity: &LaptopIdentity, name: &str) -> Device {
        Device {
            id: identity.laptop_id_hex(),
            name: name.trim().chars().take(80).collect(),
            signing: hex::encode(identity.signing_public()),
            encryption: hex::encode(identity.encryption_public()),
        }
    }

    fn id_bytes(&self) -> Result<[u8; 16], String> {
        unhex(&self.id)
    }

    fn verifying(&self) -> Result<VerifyingKey, String> {
        VerifyingKey::from_bytes(&unhex(&self.signing)?).map_err(|_| "a damaged signing key".into())
    }

    /// The short code two people compare when one laptop enrolls another.
    pub fn fingerprint(&self) -> String {
        let h = sodium::Hash::of(&[
            self.id.as_bytes(),
            self.signing.as_bytes(),
            self.encryption.as_bytes(),
        ]);
        let text = hex::encode_upper(&h[..6]);
        format!("{}-{}-{}", &text[..4], &text[4..8], &text[8..])
    }
}

/// What a new laptop shows so that an enrolled one can add it: its public keys. Typed
/// or pasted across; the fingerprint is read aloud on both sides to compare.
pub fn enrollment_code(device: &Device) -> String {
    format!(
        "FI1-{}",
        hex::encode(serde_json::to_vec(device).expect("a device serialises"))
    )
}

pub fn read_enrollment_code(code: &str) -> Result<Device, String> {
    let body = code
        .trim()
        .strip_prefix("FI1-")
        .ok_or("not an enrollment code")?;
    let bytes = hex::decode(body).map_err(|_| "a damaged enrollment code")?;
    let device: Device = serde_json::from_slice(&bytes).map_err(|_| "a damaged enrollment code")?;
    device.id_bytes()?;
    device.verifying()?;
    unhex::<32>(&device.encryption)?;
    Ok(device)
}

/// One registry event's content. Sealed grants are hex. The recovery copy is sealed to
/// the recovery key's public half: any laptop can write it, only the 24 words open it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Change {
    /// The fleet's first event: its first laptop, the recovery key's public half and a
    /// copy of the keys sealed with the recovery key.
    Create {
        device: Device,
        recovery_signing: String,
        recovery_box: String,
        grant: String,
        recovery_copy: String,
    },
    /// Another laptop joins and is given every key.
    Enroll { device: Device, grant: String },
    /// A laptop leaves. Objects it wrote up to `last_sequence` still count; a new fleet
    /// key is granted to every laptop that remains, and copied for the recovery key.
    Revoke {
        laptop: String,
        last_sequence: u64,
        grants: BTreeMap<String, String>,
        recovery_copy: String,
    },
    /// A fresh laptop let in with the recovery key, when no enrolled laptop could.
    Recover { device: Device },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Body {
    pub number: u64,
    pub previous: String,
    pub at_ms: u64,
    pub author: String,
    pub change: Change,
}

/// An event as stored: its exact signed text, and the signature over it.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub body: String,
    pub signature: String,
}

impl Event {
    fn sign(body: &Body, key: &SigningKey) -> Event {
        let text = serde_json::to_string(body).expect("a registry event serialises");
        let mut signed = SIGNED.to_vec();
        signed.extend_from_slice(text.as_bytes());
        Event {
            body: text,
            signature: hex::encode(key.sign(&signed).to_bytes()),
        }
    }

    fn signed_by(&self, key: &VerifyingKey) -> bool {
        let Ok(sig) = unhex::<64>(&self.signature) else {
            return false;
        };
        let mut signed = SIGNED.to_vec();
        signed.extend_from_slice(self.body.as_bytes());
        key.verify(&signed, &Signature::from_bytes(&sig)).is_ok()
    }

    pub fn hash(&self) -> [u8; 32] {
        sodium::Hash::of(&[self.body.as_bytes()])
    }

    /// Where the event lies on the drive.
    pub fn path(&self, number: u64) -> String {
        format!(
            "registry/{:08}-{}.json",
            number,
            hex::encode(&self.hash()[..8])
        )
    }
}

#[derive(Debug, Clone)]
pub struct Member {
    pub device: Device,
    /// The last sequence of its objects that counts, once it is revoked.
    pub revoked_after: Option<u64>,
}

/// The registry, checked: who is in the fleet, and who signed what.
#[derive(Debug, Clone)]
pub struct Registry {
    pub events: Vec<Event>,
    pub members: BTreeMap<String, Member>,
    pub recovery_signing: VerifyingKey,
    pub recovery_box: [u8; 32],
    pub fingerprint: [u8; 32],
}

impl Registry {
    /// Checks the chain from its first event. `fingerprint`, when given, is the
    /// fleet this laptop already belongs to (or was told to join): a chain with another
    /// first event is someone else's.
    pub fn verify(events: &[Event], fingerprint: Option<&[u8; 32]>) -> Result<Registry, String> {
        let first = events.first().ok_or("the registry is empty")?;
        let body: Body =
            serde_json::from_str(&first.body).map_err(|_| "a damaged registry event")?;
        let Change::Create {
            device,
            recovery_signing,
            recovery_box,
            ..
        } = &body.change
        else {
            return Err("the registry does not start with the fleet's creation".into());
        };
        if body.number != 0 || body.previous != hex::encode([0u8; 32]) || body.author != device.id {
            return Err("the registry's first event is malformed".into());
        }
        if !first.signed_by(&device.verifying()?) {
            return Err("the registry's first event is not signed by its laptop".into());
        }
        let anchor = first.hash();
        if fingerprint.is_some_and(|f| !sodium::equal(f, &anchor)) {
            return Err("this registry belongs to another fleet".into());
        }
        let recovery = VerifyingKey::from_bytes(&unhex(recovery_signing)?)
            .map_err(|_| "a damaged recovery key")?;
        let recovery_box: [u8; 32] = unhex(recovery_box)?;
        let mut members = BTreeMap::new();
        members.insert(
            device.id.clone(),
            Member {
                device: device.clone(),
                revoked_after: None,
            },
        );
        let mut previous = anchor;
        for (n, event) in events.iter().enumerate().skip(1) {
            let body: Body =
                serde_json::from_str(&event.body).map_err(|_| "a damaged registry event")?;
            if body.number != n as u64 || unhex::<32>(&body.previous)? != previous {
                return Err(format!("registry event {n} is out of its chain"));
            }
            let active = |id: &str| {
                members
                    .get(id)
                    .is_some_and(|m: &Member| m.revoked_after.is_none())
            };
            let signer = match &body.change {
                Change::Recover { device } => {
                    if body.author != device.id {
                        return Err(format!("registry event {n} is malformed"));
                    }
                    recovery
                }
                Change::Create { .. } => {
                    return Err(format!("registry event {n} creates a second fleet"));
                }
                _ if active(&body.author) => members[&body.author].device.verifying()?,
                _ => {
                    return Err(format!(
                        "registry event {n} is signed by a laptop not in the fleet"
                    ));
                }
            };
            if !event.signed_by(&signer) {
                return Err(format!("registry event {n}'s signature does not hold"));
            }
            match &body.change {
                Change::Enroll { device, .. } | Change::Recover { device } => {
                    device.id_bytes()?;
                    device.verifying()?;
                    unhex::<32>(&device.encryption)?;
                    if members.contains_key(&device.id) {
                        return Err(format!("registry event {n} enrolls a laptop twice"));
                    }
                    members.insert(
                        device.id.clone(),
                        Member {
                            device: device.clone(),
                            revoked_after: None,
                        },
                    );
                }
                Change::Revoke {
                    laptop,
                    last_sequence,
                    grants,
                    ..
                } => {
                    if !active(laptop) {
                        return Err(format!(
                            "registry event {n} revokes a laptop not in the fleet"
                        ));
                    }
                    members.get_mut(laptop).expect("checked").revoked_after = Some(*last_sequence);
                    let remaining: BTreeSet<&String> = members
                        .iter()
                        .filter(|(_, m)| m.revoked_after.is_none())
                        .map(|(k, _)| k)
                        .collect();
                    if remaining.is_empty() || grants.keys().collect::<BTreeSet<_>>() != remaining {
                        return Err(format!(
                            "registry event {n} does not give the new key to every laptop left"
                        ));
                    }
                }
                Change::Create { .. } => unreachable!(),
            }
            previous = event.hash();
        }
        Ok(Registry {
            events: events.to_vec(),
            members,
            recovery_signing: recovery,
            recovery_box,
            fingerprint: anchor,
        })
    }

    fn next(&self, author: &str, change: Change) -> Body {
        Body {
            number: self.events.len() as u64,
            previous: hex::encode(self.events.last().expect("never empty").hash()),
            at_ms: now_ms(),
            author: author.into(),
            change,
        }
    }

    fn active(&self) -> impl Iterator<Item = &Member> {
        self.members.values().filter(|m| m.revoked_after.is_none())
    }

    /// Whether `laptop` may have written object `sequence`, with this signature.
    pub fn check(
        &self,
        laptop: &[u8; 16],
        sequence: u64,
        signed: &[u8],
        signature: &[u8; 64],
    ) -> Result<(), Refusal> {
        let member = self
            .members
            .get(&hex::encode(laptop))
            .ok_or(Refusal("written by a laptop that is not enrolled"))?;
        if member.revoked_after.is_some_and(|last| sequence > last) {
            return Err(Refusal("written after its laptop was revoked"));
        }
        let key = member
            .device
            .verifying()
            .map_err(|_| Refusal("its laptop's key is damaged"))?;
        key.verify(signed, &Signature::from_bytes(signature))
            .map_err(|_| Refusal("its signature does not hold"))
    }
}

// --- the keys -------------------------------------------------------------------------

/// The keys this laptop holds. Never written anywhere but the keychain.
pub struct Keyring {
    root: Key,
    keys: BTreeMap<[u8; 16], Key>,
    current: [u8; 16],
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct KeysText {
    root: String,
    keys: BTreeMap<String, String>,
    current: String,
}

impl Drop for KeysText {
    fn drop(&mut self) {
        self.root.zeroize();
        for v in self.keys.values_mut() {
            v.zeroize();
        }
    }
}

impl std::fmt::Debug for Keyring {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Keyring")
            .field("keys", &self.keys.len())
            .finish_non_exhaustive()
    }
}

impl Keyring {
    fn new() -> Keyring {
        let current = *sodium::random::<16>();
        let mut keys = BTreeMap::new();
        keys.insert(current, sodium::random::<32>());
        Keyring {
            root: sodium::random::<32>(),
            keys,
            current,
        }
    }

    fn rotated(&self) -> Keyring {
        let current = *sodium::random::<16>();
        let mut keys: BTreeMap<[u8; 16], Key> = self
            .keys
            .iter()
            .map(|(id, k)| (*id, Zeroizing::new(**k)))
            .collect();
        keys.insert(current, sodium::random::<32>());
        Keyring {
            root: Zeroizing::new(*self.root),
            keys,
            current,
        }
    }

    fn to_text(&self) -> Zeroizing<Vec<u8>> {
        let text = KeysText {
            root: hex::encode(*self.root),
            keys: self
                .keys
                .iter()
                .map(|(id, k)| (hex::encode(id), hex::encode(**k)))
                .collect(),
            current: hex::encode(self.current),
        };
        Zeroizing::new(serde_json::to_vec(&text).expect("keys serialise"))
    }

    fn from_text(bytes: &[u8]) -> Result<Keyring, String> {
        let text: KeysText = serde_json::from_slice(bytes).map_err(|_| "damaged fleet keys")?;
        let root = Zeroizing::new(unhex::<32>(&text.root)?);
        let mut keys = BTreeMap::new();
        for (id, k) in &text.keys {
            keys.insert(unhex::<16>(id)?, Zeroizing::new(unhex::<32>(k)?));
        }
        let current = unhex::<16>(&text.current)?;
        if !keys.contains_key(&current) {
            return Err("damaged fleet keys".into());
        }
        Ok(Keyring {
            root,
            keys,
            current,
        })
    }

    fn grant(&self, device: &Device) -> Result<String, String> {
        Ok(hex::encode(sodium::seal(
            &unhex(&device.encryption)?,
            &self.to_text(),
        )))
    }

    fn recovery_copy(&self, recovery_box: &[u8; 32]) -> String {
        hex::encode(sodium::seal(recovery_box, &self.to_text()))
    }

    pub fn current(&self) -> object::FleetKey<'_> {
        object::FleetKey {
            id: self.current,
            key: &self.keys[&self.current],
        }
    }

    pub fn fleet_key(&self, id: &[u8; 16]) -> Option<&[u8; 32]> {
        self.keys.get(id).map(|k| &**k)
    }

    /// The key object names are made with: the same on every laptop, for good.
    pub fn object_keys(&self) -> Key {
        sodium::kdf(&self.root, 1, b"fi-objky")
    }

    /// The key board IDs are hashed with, once a laptop is in a fleet: the same on
    /// every laptop, so the same board is recognised wherever its logs are imported.
    pub fn board_key(&self) -> Key {
        sodium::kdf(&self.root, 2, b"fi-board")
    }

    pub fn save(&self, store: &dyn SecretStore) -> Result<(), String> {
        store.set(KEYCHAIN_ENTRY, &self.to_text())
    }

    pub fn load(store: &dyn SecretStore) -> Result<Option<Keyring>, String> {
        store
            .get(KEYCHAIN_ENTRY)?
            .map(|b| Keyring::from_text(&b))
            .transpose()
    }

    /// This laptop's keys from the registry: the latest grant sealed for it.
    pub fn from_registry(
        registry: &Registry,
        identity: &LaptopIdentity,
    ) -> Result<Keyring, String> {
        let me = identity.laptop_id_hex();
        let mut latest = None;
        for event in &registry.events {
            let body: Body =
                serde_json::from_str(&event.body).map_err(|_| "a damaged registry event")?;
            match body.change {
                Change::Create { device, grant, .. } | Change::Enroll { device, grant }
                    if device.id == me =>
                {
                    latest = Some(grant)
                }
                Change::Revoke { mut grants, .. } => {
                    if let Some(g) = grants.remove(&me) {
                        latest = Some(g);
                    }
                }
                _ => {}
            }
        }
        let grant = latest.ok_or("no keys in the registry for this laptop")?;
        let sealed = hex::decode(grant).map_err(|_| "a damaged grant")?;
        let opened = identity.open_sealed(&sealed)?;
        Keyring::from_text(&opened)
    }
}

/// Object reading against the registry and this laptop's keys.
pub struct Reader<'a> {
    pub registry: &'a Registry,
    pub keyring: &'a Keyring,
    object_keys: Key,
}

impl<'a> Reader<'a> {
    pub fn new(registry: &'a Registry, keyring: &'a Keyring) -> Self {
        Reader {
            registry,
            keyring,
            object_keys: keyring.object_keys(),
        }
    }
}

impl object::Keys for Reader<'_> {
    fn check_signature(
        &self,
        laptop: &[u8; 16],
        sequence: u64,
        signed: &[u8],
        signature: &[u8; 64],
    ) -> Result<(), Refusal> {
        self.registry.check(laptop, sequence, signed, signature)
    }
    fn fleet_key(&self, id: &[u8; 16]) -> Option<&[u8; 32]> {
        self.keyring.fleet_key(id)
    }
    fn object_keys(&self) -> &[u8; 32] {
        &self.object_keys
    }
}

// --- the recovery key -------------------------------------------------------------------

pub struct RecoveryKey(Key);

impl RecoveryKey {
    fn new() -> RecoveryKey {
        RecoveryKey(sodium::random::<32>())
    }

    fn box_secret(&self) -> Key {
        sodium::kdf(&self.0, 1, b"fi-recbx")
    }

    fn box_public(&self) -> [u8; 32] {
        x25519_dalek::PublicKey::from(&x25519_dalek::StaticSecret::from(*self.box_secret()))
            .to_bytes()
    }

    fn signing(&self) -> SigningKey {
        SigningKey::from_bytes(&sodium::kdf(&self.0, 2, b"fi-recsg"))
    }

    /// The 24 words an admin writes down: 256 bits and an 8-bit checksum (BIP-39).
    pub fn words(&self) -> Zeroizing<String> {
        let list: Vec<&str> = WORDS.lines().collect();
        let check = sodium::sha256(self.0.as_ref())[0];
        let mut bits = Zeroizing::new(Vec::with_capacity(264));
        for byte in self.0.iter().chain(std::iter::once(&check)) {
            for i in (0..8).rev() {
                bits.push((byte >> i) & 1);
            }
        }
        let mut out = Zeroizing::new(String::new());
        for (i, group) in bits.chunks(11).enumerate() {
            let index = group.iter().fold(0usize, |acc, b| (acc << 1) | *b as usize);
            if i > 0 {
                out.push(' ');
            }
            out.push_str(list[index]);
        }
        out
    }

    pub fn from_words(text: &str) -> Result<RecoveryKey, String> {
        let list: Vec<&str> = WORDS.lines().collect();
        let words: Vec<String> = text.split_whitespace().map(str::to_lowercase).collect();
        if words.len() != 24 {
            return Err("a recovery key is 24 words".into());
        }
        let mut bits = Zeroizing::new(Vec::with_capacity(264));
        for w in &words {
            let index = list
                .binary_search(&w.as_str())
                .map_err(|_| "a word that is not in the recovery word list")?;
            for i in (0..11).rev() {
                bits.push(((index >> i) & 1) as u8);
            }
        }
        let mut key = Zeroizing::new([0u8; 32]);
        for (i, byte) in bits.chunks(8).take(32).enumerate() {
            key[i] = byte.iter().fold(0u8, |acc, b| (acc << 1) | b);
        }
        let check = bits[256..].iter().fold(0u8, |acc, b| (acc << 1) | b);
        if check != sodium::sha256(key.as_ref())[0] {
            return Err("these words are not a recovery key: one is wrong or out of order".into());
        }
        Ok(RecoveryKey(key))
    }
}

// --- what a person does -------------------------------------------------------------------

/// A new fleet, with this laptop its first member. Returns the first registry event,
/// this laptop's keys, and the recovery key to show once.
pub fn create_fleet(
    identity: &LaptopIdentity,
    name: &str,
) -> Result<(Event, Keyring, RecoveryKey), String> {
    sodium::init()?;
    let keyring = Keyring::new();
    let recovery = RecoveryKey::new();
    let device = Device::of(identity, name);
    let body = Body {
        number: 0,
        previous: hex::encode([0u8; 32]),
        at_ms: now_ms(),
        author: device.id.clone(),
        change: Change::Create {
            grant: keyring.grant(&device)?,
            recovery_signing: hex::encode(recovery.signing().verifying_key().to_bytes()),
            recovery_box: hex::encode(recovery.box_public()),
            recovery_copy: keyring.recovery_copy(&recovery.box_public()),
            device,
        },
    };
    Ok((identity.sign_event(&body), keyring, recovery))
}

/// An enrolled laptop adds another, from its enrollment code.
pub fn enroll(
    registry: &Registry,
    keyring: &Keyring,
    identity: &LaptopIdentity,
    device: Device,
) -> Result<Event, String> {
    if registry.members.contains_key(&device.id) {
        return Err("this laptop is already in the fleet".into());
    }
    let change = Change::Enroll {
        grant: keyring.grant(&device)?,
        device,
    };
    Ok(identity.sign_event(&registry.next(&identity.laptop_id_hex(), change)))
}

/// Removes a laptop and rotates the fleet key. `last_sequence` is the newest of its
/// objects this laptop has applied; anything it writes after that is refused.
pub fn revoke(
    registry: &Registry,
    keyring: &Keyring,
    identity: &LaptopIdentity,
    laptop: &str,
    last_sequence: u64,
) -> Result<(Event, Keyring), String> {
    if !registry
        .members
        .get(laptop)
        .is_some_and(|m| m.revoked_after.is_none())
    {
        return Err("that laptop is not in the fleet".into());
    }
    let rotated = keyring.rotated();
    let mut grants = BTreeMap::new();
    for m in registry.active().filter(|m| m.device.id != laptop) {
        grants.insert(m.device.id.clone(), rotated.grant(&m.device)?);
    }
    if grants.is_empty() {
        return Err("a fleet needs one laptop left".into());
    }
    let change = Change::Revoke {
        laptop: laptop.into(),
        last_sequence,
        grants,
        recovery_copy: rotated.recovery_copy(&registry.recovery_box),
    };
    Ok((
        identity.sign_event(&registry.next(&identity.laptop_id_hex(), change)),
        rotated,
    ))
}

/// A fresh laptop gets the fleet's keys back from the recovery key, and an event that
/// lets it in, signed with the recovery key.
pub fn recover(
    registry: &Registry,
    words: &str,
    identity: &LaptopIdentity,
    name: &str,
) -> Result<(Event, Keyring), String> {
    let recovery = RecoveryKey::from_words(words)?;
    if recovery.signing().verifying_key() != registry.recovery_signing {
        return Err("this recovery key belongs to another fleet".into());
    }
    let mut copy = None;
    for event in &registry.events {
        let body: Body =
            serde_json::from_str(&event.body).map_err(|_| "a damaged registry event")?;
        if let Change::Create { recovery_copy, .. } | Change::Revoke { recovery_copy, .. } =
            body.change
        {
            copy = Some(recovery_copy);
        }
    }
    let sealed = hex::decode(copy.ok_or("no recovery copy in the registry")?)
        .map_err(|_| "a damaged recovery copy")?;
    let opened = sodium::seal_open(&recovery.box_public(), &recovery.box_secret(), &sealed)?;
    let keyring = Keyring::from_text(&opened)?;
    let device = Device::of(identity, name);
    if registry.members.contains_key(&device.id) {
        return Err("this laptop is already in the fleet".into());
    }
    let author = device.id.clone();
    let body = registry.next(&author, Change::Recover { device });
    Ok((Event::sign(&body, &recovery.signing()), keyring))
}

impl LaptopIdentity {
    fn sign_event(&self, body: &Body) -> Event {
        let text = serde_json::to_string(body).expect("a registry event serialises");
        let mut signed = SIGNED.to_vec();
        signed.extend_from_slice(text.as_bytes());
        Event {
            body: text,
            signature: hex::encode(self.sign(&signed)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;
    use crate::sync::object::{read, write};

    fn laptop() -> LaptopIdentity {
        LaptopIdentity::load_or_create(&MemoryStore::default())
            .unwrap()
            .0
    }

    struct Fleet {
        events: Vec<Event>,
        recovery: RecoveryKey,
    }

    impl Fleet {
        fn registry(&self) -> Registry {
            Registry::verify(&self.events, None).unwrap()
        }
    }

    fn object_from(
        identity: &LaptopIdentity,
        keyring: &Keyring,
        sequence: u64,
        text: &[u8],
    ) -> (Vec<u8>, object::Name) {
        let mut out = Vec::new();
        let id: [u8; 16] = unhex(&identity.laptop_id_hex()).unwrap();
        let name = write(
            &mut out,
            &mut &text[..],
            id,
            sequence,
            &keyring.current(),
            &keyring.object_keys(),
            &|m| identity.sign(m),
        )
        .unwrap(); // fmt: skip
        (out, name)
    }

    fn read_as(
        registry: &Registry,
        keyring: &Keyring,
        bytes: &[u8],
        name: &object::Name,
    ) -> Result<Vec<u8>, Refusal> {
        let dir = std::env::temp_dir().join(format!(
            "kr-{}",
            hex::encode(sodium::random::<8>().as_ref())
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("o.fio");
        std::fs::write(&path, bytes).unwrap();
        let mut out = Vec::new();
        let got = read(&path, name, &Reader::new(registry, keyring), &mut out).map(|()| out);
        std::fs::remove_dir_all(&dir).unwrap();
        got
    }

    #[test]
    fn the_word_list_is_bip39_english() {
        let digest = sodium::sha256(WORDS.as_bytes());
        assert_eq!(
            hex::encode(digest),
            "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
        );
        let list: Vec<&str> = WORDS.lines().collect();
        assert_eq!(list.len(), 2048);
        assert!(
            list.windows(2).all(|w| w[0] < w[1]),
            "sorted, for the binary search"
        );
    }

    #[test]
    fn recovery_words_round_trip_and_catch_mistakes() {
        // BIP-39's published vector: 32 bytes of 0x7f
        let known = RecoveryKey(Zeroizing::new([0x7f; 32]));
        assert_eq!(
            known.words().as_str(),
            "legal winner thank year wave sausage worth useful legal winner thank year wave \
             sausage worth useful legal winner thank year wave sausage worth title"
        );
        let key = RecoveryKey::new();
        let words = key.words();
        assert_eq!(
            *RecoveryKey::from_words(&words.to_uppercase()).unwrap().0,
            *key.0
        );
        // two words out of order: on the published vector, whose checksum this swap breaks
        // (a random key's 8-bit checksum lets one swap in 256 through, so not on that)
        let known_words = known.words();
        let mut swapped: Vec<&str> = known_words.split(' ').collect();
        swapped.swap(3, 4);
        assert!(RecoveryKey::from_words(&swapped.join(" ")).is_err());
        assert!(RecoveryKey::from_words("legal winner").is_err());
        let typo = words.replacen(words.split(' ').next().unwrap(), "zzzz", 1);
        assert!(RecoveryKey::from_words(&typo).is_err());
    }

    fn new_fleet(first: &LaptopIdentity) -> (Fleet, Keyring) {
        let (event, keys, recovery) = create_fleet(first, "Bench laptop").unwrap();
        (
            Fleet {
                events: vec![event],
                recovery,
            },
            keys,
        )
    }

    #[test]
    fn enrollment_gives_a_laptop_every_key() {
        let (a, b) = (laptop(), laptop());
        let (mut fleet, keys_a) = new_fleet(&a);
        let code = enrollment_code(&Device::of(&b, "Field laptop"));
        let device = read_enrollment_code(&code).unwrap();
        assert_eq!(
            device.fingerprint(),
            Device::of(&b, "Field laptop").fingerprint()
        );
        fleet
            .events
            .push(enroll(&fleet.registry(), &keys_a, &a, device).unwrap());
        let registry = fleet.registry();
        let keys_b = Keyring::from_registry(&registry, &b).unwrap();
        assert_eq!(*keys_b.object_keys(), *keys_a.object_keys());
        assert_eq!(*keys_b.board_key(), *keys_a.board_key());
        // B reads what A wrote, and A what B wrote
        let (bytes, name) = object_from(&a, &keys_a, 1, b"from A");
        assert_eq!(
            read_as(&registry, &keys_b, &bytes, &name).unwrap(),
            b"from A"
        );
        let (bytes, name) = object_from(&b, &keys_b, 1, b"from B");
        assert_eq!(
            read_as(&registry, &keys_a, &bytes, &name).unwrap(),
            b"from B"
        );
        // and the keys survive the keychain
        let store = MemoryStore::default();
        keys_b.save(&store).unwrap();
        let back = Keyring::load(&store).unwrap().unwrap();
        assert_eq!(*back.object_keys(), *keys_b.object_keys());
    }

    #[test]
    fn a_revoked_laptop_reads_nothing_new_and_its_later_objects_are_refused() {
        let (a, b, c) = (laptop(), laptop(), laptop());
        let (mut fleet, keys_a) = new_fleet(&a);
        for x in [&b, &c] {
            let e = enroll(&fleet.registry(), &keys_a, &a, Device::of(x, "x")).unwrap();
            fleet.events.push(e);
        }
        let registry = fleet.registry();
        let keys_c = Keyring::from_registry(&registry, &c).unwrap();
        let (before, before_name) = object_from(&c, &keys_c, 5, b"C, before");
        let (after, after_name) = object_from(&c, &keys_c, 6, b"C, after");
        let (event, keys_a2) = revoke(&registry, &keys_a, &a, &c.laptop_id_hex(), 5).unwrap();
        fleet.events.push(event);
        let registry = fleet.registry();
        let keys_b2 = Keyring::from_registry(&registry, &b).unwrap();
        assert_eq!(keys_b2.current().id, keys_a2.current().id);
        assert_ne!(keys_a2.current().id, keys_c.current().id);
        // C's objects up to its last counted sequence still read; later ones do not
        assert_eq!(
            read_as(&registry, &keys_b2, &before, &before_name).unwrap(),
            b"C, before"
        );
        assert_eq!(
            read_as(&registry, &keys_b2, &after, &after_name),
            Err(Refusal("written after its laptop was revoked"))
        );
        // what A writes now, C cannot open
        let (bytes, name) = object_from(&a, &keys_a2, 9, b"after the rotation");
        assert_eq!(
            read_as(&registry, &keys_c, &bytes, &name),
            Err(Refusal(
                "written with a fleet key this laptop does not hold"
            ))
        );
        assert!(
            Keyring::from_registry(&registry, &c)
                .unwrap()
                .fleet_key(&keys_a2.current().id)
                .is_none()
        );
        // and C can no longer sign registry events
        let d = laptop();
        let forged = enroll(&registry, &keys_c, &c, Device::of(&d, "d")).unwrap();
        let mut events = fleet.events.clone();
        events.push(forged);
        assert!(
            Registry::verify(&events, None)
                .unwrap_err()
                .contains("not in the fleet")
        );
    }

    #[test]
    fn the_recovery_key_alone_lets_a_fresh_laptop_back_in() {
        let (a, b) = (laptop(), laptop());
        let (mut fleet, keys_a) = new_fleet(&a);
        fleet
            .events
            .push(enroll(&fleet.registry(), &keys_a, &a, Device::of(&b, "b")).unwrap());
        let (event, keys_a2) =
            revoke(&fleet.registry(), &keys_a, &a, &b.laptop_id_hex(), 0).unwrap();
        fleet.events.push(event);
        let (old, old_name) = object_from(&a, &keys_a, 1, b"before the rotation");
        let (new, new_name) = object_from(&a, &keys_a2, 2, b"after the rotation");
        // every laptop is lost; a fresh one has the words and the drive
        let fresh = laptop();
        let words = fleet.recovery.words();
        let (event, keys) = recover(&fleet.registry(), &words, &fresh, "Replacement").unwrap();
        fleet.events.push(event);
        let registry =
            Registry::verify(&fleet.events, Some(&fleet.registry().fingerprint)).unwrap();
        assert!(registry.members.contains_key(&fresh.laptop_id_hex()));
        assert_eq!(
            read_as(&registry, &keys, &old, &old_name).unwrap(),
            b"before the rotation"
        );
        assert_eq!(
            read_as(&registry, &keys, &new, &new_name).unwrap(),
            b"after the rotation"
        );
        // and it can enroll others from here
        let next = laptop();
        fleet
            .events
            .push(enroll(&registry, &keys, &fresh, Device::of(&next, "n")).unwrap());
        assert!(Registry::verify(&fleet.events, None).is_ok());
        // another fleet's words do not work
        let (other, _) = new_fleet(&laptop());
        assert!(recover(&fleet.registry(), &other.recovery.words(), &laptop(), "x").is_err());
    }

    #[test]
    fn a_tampered_or_foreign_registry_is_refused() {
        let (a, b) = (laptop(), laptop());
        let (mut fleet, keys_a) = new_fleet(&a);
        fleet
            .events
            .push(enroll(&fleet.registry(), &keys_a, &a, Device::of(&b, "b")).unwrap());
        // an edited event
        let mut events = fleet.events.clone();
        events[1].body = events[1].body.replace("\"b\"", "\"c\"");
        assert!(Registry::verify(&events, None).is_err());
        // an event dropped from the chain
        let d = laptop();
        let later = enroll(&fleet.registry(), &keys_a, &a, Device::of(&d, "d")).unwrap();
        let events = vec![fleet.events[0].clone(), later];
        assert!(Registry::verify(&events, None).is_err());
        // a stranger signs an enrollment
        let stranger = laptop();
        let forged = enroll(&fleet.registry(), &keys_a, &stranger, Device::of(&d, "d")).unwrap();
        let mut events = fleet.events.clone();
        events.push(forged);
        assert!(
            Registry::verify(&events, None)
                .unwrap_err()
                .contains("not in the fleet")
        );
        // another fleet's chain, checked against ours
        let (other, _) = new_fleet(&laptop());
        let ours = fleet.registry().fingerprint;
        assert!(
            Registry::verify(&other.events, Some(&ours))
                .unwrap_err()
                .contains("another fleet")
        );
        // a damaged signature
        let mut events = fleet.events.clone();
        events[0].signature.push('0');
        assert!(Registry::verify(&events, None).is_err());
    }

    #[test]
    fn keys_never_show_in_debug_output() {
        let (_, keys) = new_fleet(&laptop());
        let shown = format!("{keys:?}");
        assert!(!shown.contains(&hex::encode(*keys.object_keys())));
        assert!(!shown.contains(&hex::encode(keys.current().key)));
    }
}
