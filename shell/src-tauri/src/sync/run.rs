//! The sync loop (spec: Shared storage and sync, "Checking incoming data" and
//! "Uploading"). One round sends what waits in the engine's outbox and applies what
//! other laptops wrote, through any `Drive`.
//!
//! **Up:** each outbox object becomes a plaintext (a header line, then for a source
//! file its bytes), which is sealed and signed into an `.fio` in staging, uploaded
//! under its name, compared with the checksum the drive reports, and only then marked
//! sent. A sealed object stays in staging until it is marked sent, so a retry uploads
//! the same bytes. A source file is read from where it was imported and must still
//! have its SHA-256; one that moved waits. When the same content is already on the
//! drive, a small empty records object takes its sequence number instead, so no laptop
//! sees a gap and the bytes go up once.
//!
//! **Down:** every object on the drive whose key this laptop has not seen is
//! downloaded to staging, checked (signer, encryption, name) and handed to the engine,
//! each laptop's objects in sequence order. A refusal goes to quarantine with its
//! reason; an object the engine cannot apply yet is offered again next round. A
//! sequence number missing from a laptop's folder is reported as a gap.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::identity::LaptopIdentity;
use crate::sync::drive::Drive;
use crate::sync::keyring::{Body, Event, Keyring, Reader, Registry};
use crate::sync::object::{self, Name};
use crate::sync::sodium::{self, Sha256};

const RECORDS_FORMAT: &str = "fi.sync.records/1";
const SOURCE_FORMAT: &str = "fi.sync.source/1";
const BATCH: u64 = 50;

/// The engine, as the sync loop calls it.
pub trait Calls {
    fn call(&self, method: &str, params: Value) -> Result<Value, String>;
}

impl Calls for crate::engine::Engine {
    fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.client
            .call(method, params, crate::engine::CALL_TIMEOUT)
            .map_err(|e| e.to_string())
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Round {
    pub uploaded: usize,
    /// Source files whose content another laptop had already shared.
    pub already_shared: usize,
    /// Source files that moved or changed since they were imported.
    pub sources_moved: usize,
    pub applied: usize,
    pub waiting: usize,
    pub quarantined: usize,
    /// Sequence numbers missing from another laptop's folder on the drive: an object
    /// lost or still uploading. Shown, and waited for.
    pub gaps: Vec<(String, u64)>,
}

pub struct Syncer<'a> {
    pub engine: &'a dyn Calls,
    pub drive: &'a dyn Drive,
    pub identity: &'a LaptopIdentity,
    pub registry: &'a Registry,
    pub keyring: &'a Keyring,
    /// This laptop's own folder for objects on their way up or down.
    pub staging: PathBuf,
}

fn io_err(e: io::Error) -> String {
    format!("staging: {e}")
}

/// Writes the plaintext's header line, then passes the rest to a file.
struct Split {
    header: Vec<u8>,
    in_header: bool,
    body: Option<BufWriter<File>>,
}

impl Write for Split {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let mut rest = buf;
        if self.in_header {
            match rest.iter().position(|b| *b == b'\n') {
                Some(i) => {
                    self.header.extend_from_slice(&rest[..i]);
                    self.in_header = false;
                    rest = &rest[i + 1..];
                }
                None => {
                    self.header.extend_from_slice(rest);
                    if self.header.len() > 64 << 20 {
                        return Err(io::Error::other("a header past any sensible size"));
                    }
                    return Ok(buf.len());
                }
            }
        }
        if !rest.is_empty() {
            match &mut self.body {
                Some(body) => body.write_all(rest)?,
                None => return Err(io::Error::other("a records object with a body")),
            }
        }
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        if let Some(body) = &mut self.body {
            body.flush()?;
        }
        Ok(())
    }
}

impl Syncer<'_> {
    fn laptop(&self) -> [u8; 16] {
        let mut id = [0u8; 16];
        hex::decode_to_slice(self.identity.laptop_id_hex(), &mut id).expect("a laptop ID is hex");
        id
    }

    fn dir(&self, name: &str) -> Result<PathBuf, String> {
        let dir = self.staging.join(name);
        fs::create_dir_all(&dir).map_err(io_err)?;
        Ok(dir)
    }

    /// Seals a plaintext (a header line and an optional file) into staging.
    fn seal(
        &self,
        sequence: u64,
        header: &[u8],
        body: Option<&Path>,
        out: &Path,
    ) -> Result<Name, String> {
        let mut plain: Box<dyn Read> = Box::new(io::Cursor::new([header, b"\n"].concat()));
        if let Some(path) = body {
            plain = Box::new(plain.chain(File::open(path).map_err(io_err)?));
        }
        let partial = out.with_extension("partial");
        let mut file = BufWriter::new(File::create(&partial).map_err(io_err)?);
        let name = object::write(
            &mut file,
            &mut plain,
            self.laptop(),
            sequence,
            &self.keyring.current(),
            &self.keyring.object_keys(),
            &|m| self.identity.sign(m),
        )
        .map_err(io_err)?;
        drop(file);
        fs::rename(&partial, out).map_err(io_err)?;
        fs::write(out.with_extension("name"), name.path()).map_err(io_err)?;
        Ok(name)
    }

    /// Uploads a sealed object and checks what the drive holds. An object already at
    /// that path counts as uploaded only if it is these very bytes.
    fn put(&self, name: &Name, sealed: &Path) -> Result<(), String> {
        let ours = Sha256::of_file(sealed).map_err(io_err)?;
        let theirs = match self.drive.upload(&name.path(), sealed) {
            Ok(sum) => sum,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                let back = self.dir("check")?.join("object");
                self.drive.download(&name.path(), &back).map_err(io_err)?;
                let sum = Sha256::of_file(&back).map_err(io_err)?;
                let _ = fs::remove_file(&back);
                sum
            }
            Err(e) => return Err(format!("the upload failed: {e}")),
        };
        if !sodium::equal(&ours, &theirs) {
            return Err("the drive holds different bytes than were uploaded".into());
        }
        Ok(())
    }

    fn on_drive(&self, object_key: &[u8; 32]) -> Result<bool, String> {
        let suffix = format!("-{}.fio", hex::encode(object_key));
        Ok(self
            .drive
            .list("devices/")
            .map_err(|e| format!("the drive could not be listed: {e}"))?
            .iter()
            .any(|e| e.path.ends_with(&suffix)))
    }

    /// Sends what waits in the outbox.
    pub fn upload(&self, round: &mut Round) -> Result<(), String> {
        let out = self.dir("out")?;
        let waiting = self.engine.call("sync.outbox", json!({ "limit": BATCH }))?;
        for o in waiting.as_array().ok_or("the outbox is not a list")? {
            let sequence = o["sequence"]
                .as_u64()
                .ok_or("an outbox object without a sequence")?;
            let sealed = out.join(format!("{sequence:012}.fio"));
            let name = if sealed.is_file() {
                // sealed by an earlier round: the same bytes go up again
                let path = fs::read_to_string(sealed.with_extension("name")).map_err(io_err)?;
                Name::parse(&path).ok_or("a damaged staged object")?
            } else if o["kind"] == "source" {
                let mut manifest = o["payload"].clone();
                let path = manifest
                    .as_object_mut()
                    .and_then(|m| m.remove("path"))
                    .and_then(|p| p.as_str().map(PathBuf::from))
                    .ok_or("a source object without its path")?;
                let want = manifest["sha256"].as_str().unwrap_or_default().to_string();
                if !path.is_file() || hex::encode(Sha256::of_file(&path).map_err(io_err)?) != want {
                    round.sources_moved += 1;
                    continue;
                }
                let header = serde_json::to_vec(&manifest).map_err(|e| e.to_string())?;
                let name = self.seal(sequence, &header, Some(&path), &sealed)?;
                if self.on_drive(&name.object_key)? {
                    round.already_shared += 1;
                    let same = json!({ "format": RECORDS_FORMAT, "laptop": self.identity.laptop_id_hex(),
                                       "entries": [], "same_as": hex::encode(name.object_key) });
                    fs::remove_file(&sealed).map_err(io_err)?;
                    self.seal(
                        sequence,
                        &serde_json::to_vec(&same).map_err(|e| e.to_string())?,
                        None,
                        &sealed,
                    )?
                } else {
                    name
                }
            } else {
                let header = serde_json::to_vec(&o["payload"]).map_err(|e| e.to_string())?;
                self.seal(sequence, &header, None, &sealed)?
            };
            self.put(&name, &sealed)?;
            self.engine.call(
                "sync.sent",
                json!({ "sequence": sequence, "object_key": hex::encode(name.object_key) }),
            )?;
            let _ = fs::remove_file(sealed.with_extension("name"));
            fs::remove_file(&sealed).map_err(io_err)?;
            round.uploaded += 1;
        }
        Ok(())
    }

    /// Applies what other laptops wrote.
    pub fn download(&self, round: &mut Round) -> Result<(), String> {
        let me = self.laptop();
        let mut found: Vec<Name> = self
            .drive
            .list("devices/")
            .map_err(|e| format!("the drive could not be listed: {e}"))?
            .iter()
            .filter_map(|e| Name::parse(&e.path))
            .filter(|n| n.laptop != me)
            .collect();
        found.sort_by_key(|n| (n.laptop, n.sequence));
        if found.is_empty() {
            return Ok(());
        }
        let mut have: BTreeMap<[u8; 16], Vec<u64>> = BTreeMap::new();
        for n in &found {
            have.entry(n.laptop).or_default().push(n.sequence);
        }
        for (laptop, seqs) in &have {
            let top = seqs.iter().copied().max().unwrap_or(0);
            let missing = (1..top).filter(|s| seqs.binary_search(s).is_err());
            round.gaps.extend(missing.map(|s| (hex::encode(laptop), s)));
        }
        let keys: Vec<String> = found.iter().map(|n| hex::encode(n.object_key)).collect();
        let unknown = self
            .engine
            .call("sync.unknown", json!({ "object_keys": keys }))?;
        let unknown: std::collections::HashSet<&str> = unknown
            .as_array()
            .ok_or("not a list")?
            .iter()
            .filter_map(Value::as_str)
            .collect();
        let dir = self.dir("in")?;
        let mut seen = BTreeMap::new();
        for name in found {
            let key = hex::encode(name.object_key);
            if !unknown.contains(key.as_str()) || seen.insert(key.clone(), ()).is_some() {
                continue;
            }
            let fio = dir.join(format!("{key}.fio"));
            let body = dir.join(format!("{key}.body"));
            let state = self.take(&name, &key, &fio, &body);
            let _ = fs::remove_file(&fio);
            let _ = fs::remove_file(&body);
            match state?.as_str() {
                "applied" => round.applied += 1,
                "waiting" => round.waiting += 1,
                _ => round.quarantined += 1,
            }
        }
        Ok(())
    }

    fn take(&self, name: &Name, key: &str, fio: &Path, body: &Path) -> Result<String, String> {
        let laptop = hex::encode(name.laptop);
        let quarantine = |reason: &str| -> Result<String, String> {
            self.engine.call(
                "sync.quarantine",
                json!({ "object_key": key, "laptop": laptop, "sequence": name.sequence, "reason": reason }),
            )?;
            Ok("quarantined".into())
        };
        self.drive
            .download(&name.path(), fio)
            .map_err(|e| format!("the download failed: {e}"))?;
        let mut split = Split {
            header: Vec::new(),
            in_header: true,
            body: Some(BufWriter::new(File::create(body).map_err(io_err)?)),
        };
        if let Err(refusal) = object::read(
            fio,
            name,
            &Reader::new(self.registry, self.keyring),
            &mut split,
        ) {
            return quarantine(refusal.0);
        }
        split.flush().map_err(io_err)?;
        drop(split.body.take());
        let Ok(header) = serde_json::from_slice::<Value>(&split.header) else {
            return quarantine("its header is not JSON");
        };
        let applied = match header["format"].as_str() {
            Some(RECORDS_FORMAT) => self.engine.call(
                "sync.apply",
                json!({ "object_key": key, "laptop": laptop, "sequence": name.sequence,
                        "kind": "records", "payload": header }),
            )?,
            Some(SOURCE_FORMAT) => self.engine.call(
                "sync.apply",
                json!({ "object_key": key, "laptop": laptop, "sequence": name.sequence,
                        "kind": "source", "payload": header, "path": body.to_string_lossy() }),
            )?,
            _ => return quarantine("not an object kind this app reads"),
        };
        Ok(applied["state"]
            .as_str()
            .unwrap_or("quarantined")
            .to_string())
    }

    /// A round that looks at the drive only when its change feed says something
    /// arrived, or when `force` (objects still wait for one another); `cursor` keeps
    /// the feed's place between rounds.
    pub fn round_watching(
        &self,
        cursor: &mut Option<String>,
        force: bool,
    ) -> Result<Round, String> {
        let mut round = Round::default();
        self.upload(&mut round)?;
        let (any, next) = self
            .drive
            .changes(cursor.as_deref())
            .map_err(|e| format!("the drive's change feed: {e}"))?;
        if any || force || next.is_none() {
            self.download(&mut round)?;
        }
        *cursor = next;
        Ok(round)
    }

    /// One round: up, then down.
    pub fn round(&self) -> Result<Round, String> {
        let mut round = Round::default();
        self.upload(&mut round)?;
        self.download(&mut round)?;
        Ok(round)
    }
}

// --- the registry on the drive ---------------------------------------------------------

/// Adds a registry event as the next file in `registry/`.
pub fn publish(
    drive: &dyn Drive,
    staging: &Path,
    event: &Event,
    number: u64,
) -> Result<(), String> {
    fs::create_dir_all(staging).map_err(io_err)?;
    let file = staging.join(format!("event-{number}.json"));
    fs::write(&file, serde_json::to_vec(event).map_err(|e| e.to_string())?).map_err(io_err)?;
    let done = drive
        .upload(&event.path(number), &file)
        .map_err(|e| format!("the registry event could not be written: {e}"));
    let _ = fs::remove_file(&file);
    done.map(|_| ())
}

/// The registry as the drive holds it, checked from its first event.
/// The registry as the drive holds it, checked from its first event.
///
/// Two laptops can write event N at the same moment (Drive keeps both files). At each
/// number the chain takes the candidate that follows the event before it, and of two
/// that both do, the one with the smaller hash: every laptop picks the same, and the
/// other laptop writes its event again at the next number (`publish_next`).
pub fn read_registry(
    drive: &dyn Drive,
    staging: &Path,
    fingerprint: Option<&[u8; 32]>,
) -> Result<Registry, String> {
    fs::create_dir_all(staging).map_err(io_err)?;
    let mut by_number: BTreeMap<u64, Vec<Event>> = BTreeMap::new();
    for entry in drive
        .list("registry/")
        .map_err(|e| format!("the drive could not be listed: {e}"))?
    {
        let number = entry
            .path
            .strip_prefix("registry/")
            .and_then(|n| n.split('-').next())
            .and_then(|n| n.parse::<u64>().ok())
            .ok_or("a file in the registry that is not an event")?;
        let file = staging.join("event.json");
        drive.download(&entry.path, &file).map_err(io_err)?;
        let text = fs::read(&file).map_err(io_err)?;
        let _ = fs::remove_file(&file);
        let event: Event = serde_json::from_slice(&text).map_err(|_| "a damaged registry event")?;
        if event.path(number) != entry.path {
            return Err("a registry file whose name does not match its event".into());
        }
        by_number.entry(number).or_default().push(event);
    }
    let mut events: Vec<Event> = Vec::new();
    for n in 0u64.. {
        let Some(candidates) = by_number.get(&n) else {
            break;
        };
        let follows = |e: &&Event| -> bool {
            let Ok(body) = serde_json::from_str::<Body>(&e.body) else {
                return false;
            };
            match events.last() {
                Some(prev) => body.previous == hex::encode(prev.hash()),
                None => fingerprint.is_none_or(|f| e.hash() == *f),
            }
        };
        let Some(next) = candidates.iter().filter(follows).min_by_key(|e| e.hash()) else {
            break;
        };
        events.push(next.clone());
    }
    Registry::verify(&events, fingerprint)
}

/// Writes the event `make` builds on the registry as it stands, and checks that it made
/// it into the chain; when another laptop's event took that number first, builds it
/// again on the new registry and writes it at the next number.
pub fn publish_next(
    drive: &dyn Drive,
    staging: &Path,
    fingerprint: &[u8; 32],
    make: &dyn Fn(&Registry) -> Result<Event, String>,
) -> Result<Registry, String> {
    for _ in 0..5 {
        let registry = read_registry(drive, staging, Some(fingerprint))?;
        let event = make(&registry)?;
        publish(drive, staging, &event, registry.events.len() as u64)?;
        let after = read_registry(drive, staging, Some(fingerprint))?;
        if after.events.iter().any(|e| e.hash() == event.hash()) {
            return Ok(after);
        }
    }
    Err("the registry kept changing under this write: try again".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;
    use crate::sync::drive::FolderDrive;
    use crate::sync::keyring::{Device, create_fleet, enroll};
    use std::cell::RefCell;

    /// An engine stand-in that keeps what it is given, for the loop's own logic. The
    /// real engines' side runs in engine.rs's tests.
    /// An applied object: its key, payload and, for a source, the file's bytes.
    type Applied = (String, Value, Option<Vec<u8>>);

    #[derive(Default)]
    struct Fake {
        outbox: RefCell<Vec<Value>>,
        sent: RefCell<Vec<(u64, String)>>,
        known: RefCell<Vec<String>>,
        applied: RefCell<Vec<Applied>>,
        quarantined: RefCell<Vec<(String, String)>>,
    }

    impl Calls for Fake {
        fn call(&self, method: &str, p: Value) -> Result<Value, String> {
            match method {
                "sync.outbox" => {
                    let sent: Vec<u64> = self.sent.borrow().iter().map(|s| s.0).collect();
                    Ok(Value::Array(
                        self.outbox
                            .borrow()
                            .iter()
                            .filter(|o| !sent.contains(&o["sequence"].as_u64().unwrap()))
                            .cloned()
                            .collect(),
                    ))
                }
                "sync.sent" => {
                    let key = p["object_key"].as_str().unwrap().to_string();
                    self.known.borrow_mut().push(key.clone());
                    self.sent
                        .borrow_mut()
                        .push((p["sequence"].as_u64().unwrap(), key));
                    Ok(json!({}))
                }
                "sync.unknown" => {
                    let known = self.known.borrow();
                    Ok(Value::Array(
                        p["object_keys"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .filter(|k| !known.contains(&k.as_str().unwrap().to_string()))
                            .cloned()
                            .collect(),
                    ))
                }
                "sync.apply" => {
                    let key = p["object_key"].as_str().unwrap().to_string();
                    self.known.borrow_mut().push(key.clone());
                    let body = p["path"].as_str().map(|path| fs::read(path).unwrap());
                    self.applied
                        .borrow_mut()
                        .push((key, p["payload"].clone(), body));
                    Ok(json!({ "state": "applied" }))
                }
                "sync.quarantine" => {
                    let key = p["object_key"].as_str().unwrap().to_string();
                    self.known.borrow_mut().push(key.clone());
                    self.quarantined
                        .borrow_mut()
                        .push((key, p["reason"].as_str().unwrap().into()));
                    Ok(json!({ "state": "quarantined" }))
                }
                _ => Err(format!("no method {method}")),
            }
        }
    }

    fn temp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "fi-run-{name}-{}",
            hex::encode(sodium::random::<6>().as_ref())
        ))
    }

    fn laptop() -> LaptopIdentity {
        LaptopIdentity::load_or_create(&MemoryStore::default())
            .unwrap()
            .0
    }

    #[test]
    fn records_and_sources_travel_and_tampering_is_quarantined() {
        let root = temp("drive");
        let drive = FolderDrive::new(&root).unwrap();
        let (a, b) = (laptop(), laptop());
        let (first, keys_a, _) = create_fleet(&a, "A").unwrap();
        let staging = temp("staging");
        publish(&drive, &staging, &first, 0).unwrap();
        let reg = read_registry(&drive, &staging, None).unwrap();
        publish(
            &drive,
            &staging,
            &enroll(&reg, &keys_a, &a, Device::of(&b, "B")).unwrap(),
            1,
        )
        .unwrap();
        let registry = read_registry(&drive, &staging, Some(&reg.fingerprint)).unwrap();
        let keys_b = Keyring::from_registry(&registry, &b).unwrap();

        let log = temp("log.bin");
        fs::write(&log, b"\x01\x02 log bytes\n with a newline").unwrap();
        let sha = hex::encode(Sha256::of_file(&log).unwrap());
        let engine_a = Fake::default();
        engine_a.outbox.borrow_mut().extend([
            json!({ "sequence": 1, "kind": "records", "payload": { "format": RECORDS_FORMAT, "laptop": a.laptop_id_hex(), "entries": [{"op": "put"}] } }),
            json!({ "sequence": 2, "kind": "source", "payload": { "format": SOURCE_FORMAT, "sha256": sha, "kind": "dataflash", "size": 30, "path": log.to_string_lossy() } }),
        ]);
        let sync_a = Syncer {
            engine: &engine_a,
            drive: &drive,
            identity: &a,
            registry: &registry,
            keyring: &keys_a,
            staging: temp("sa"),
        };
        let r = sync_a.round().unwrap();
        assert_eq!((r.uploaded, r.applied), (2, 0));
        assert_eq!(sync_a.round().unwrap(), Round::default(), "nothing twice");

        let engine_b = Fake::default();
        let sync_b = Syncer {
            engine: &engine_b,
            drive: &drive,
            identity: &b,
            registry: &registry,
            keyring: &keys_b,
            staging: temp("sb"),
        };
        let r = sync_b.round().unwrap();
        assert_eq!((r.applied, r.quarantined), (2, 0));
        let applied = engine_b.applied.borrow();
        assert_eq!(applied[0].1["entries"][0]["op"], "put");
        assert!(applied[1].1.get("path").is_none(), "the path never travels");
        assert_eq!(applied[1].2.as_deref(), Some(&fs::read(&log).unwrap()[..]));
        drop(applied);

        // B imports the same log: its bytes are not uploaded again, and B's sequence
        // still has an object
        engine_b.outbox.borrow_mut().push(json!({ "sequence": 1, "kind": "source", "payload": { "format": SOURCE_FORMAT, "sha256": sha, "kind": "dataflash", "size": 30, "path": log.to_string_lossy() } }));
        let r = sync_b.round().unwrap();
        assert_eq!((r.uploaded, r.already_shared), (1, 1));
        let r = sync_a.round().unwrap();
        assert_eq!(r.applied, 1);
        assert_eq!(
            engine_a.applied.borrow()[0].1["same_as"]
                .as_str()
                .unwrap()
                .len(),
            64
        );

        // a tampered object lands in quarantine with its reason, and is not deleted
        let victim = drive
            .list("devices/")
            .unwrap()
            .into_iter()
            .find(|e| e.path.contains(&a.laptop_id_hex()))
            .unwrap();
        let mut bytes = fs::read(root.join(&victim.path)).unwrap();
        bytes[200] ^= 1;
        // a known object copied under its own key is skipped as known; under a new
        // name it is checked, and refused
        let forged = Name {
            sequence: 9,
            object_key: *sodium::random::<32>(),
            ..Name::parse(&victim.path).unwrap()
        };
        fs::create_dir_all(root.join(forged.path()).parent().unwrap()).unwrap();
        fs::write(root.join(forged.path()), &bytes).unwrap();
        let r = sync_b.round().unwrap();
        assert_eq!(r.quarantined, 1);
        assert_eq!(
            engine_b.quarantined.borrow()[0].1,
            "its name and its header disagree"
        );
        assert!(root.join(forged.path()).is_file());

        // a source file that moved waits, and is sent once it is back
        let moved = temp("moved.bin");
        fs::rename(&log, &moved).unwrap();
        engine_a.outbox.borrow_mut().push(json!({ "sequence": 3, "kind": "source", "payload": { "format": SOURCE_FORMAT, "sha256": sha, "kind": "dataflash", "size": 30, "path": log.to_string_lossy() } }));
        assert_eq!(sync_a.round().unwrap().sources_moved, 1);
        fs::rename(&moved, &log).unwrap();
        assert_eq!(sync_a.round().unwrap().uploaded, 1);

        for p in [&root, &staging, &sync_a.staging, &sync_b.staging, &log] {
            let _ = fs::remove_dir_all(p);
            let _ = fs::remove_file(p);
        }
    }

    #[test]
    fn two_laptops_enrolling_at_once_both_get_in() {
        let root = temp("race");
        let drive = FolderDrive::new(&root).unwrap();
        let (a, b) = (laptop(), laptop());
        let (first, keys_a, _) = create_fleet(&a, "A").unwrap();
        let staging = temp("race-staging");
        publish(&drive, &staging, &first, 0).unwrap();
        let fp = read_registry(&drive, &staging, None).unwrap().fingerprint;
        // A enrolls B and C at the same moment, from two windows: both write event 1
        let base = read_registry(&drive, &staging, Some(&fp)).unwrap();
        let (c, d) = (laptop(), laptop());
        let to_c = enroll(&base, &keys_a, &a, Device::of(&c, "C")).unwrap();
        let to_d = enroll(&base, &keys_a, &a, Device::of(&d, "D")).unwrap();
        publish(&drive, &staging, &to_c, 1).unwrap();
        publish(&drive, &staging, &to_d, 1).unwrap();
        let reg = read_registry(&drive, &staging, Some(&fp)).unwrap();
        assert_eq!(reg.events.len(), 2, "one of the two is in the chain");
        let won = reg.events[1].hash();
        assert_eq!(
            won,
            to_c.hash().min(to_d.hash()),
            "the smaller hash, on every laptop"
        );
        // the one that lost is written again on top, and both laptops are in
        let lost = if won == to_c.hash() { &d } else { &c };
        let reg = publish_next(&drive, &staging, &fp, &|r| {
            enroll(r, &keys_a, &a, Device::of(lost, "again"))
        })
        .unwrap();
        assert_eq!(reg.events.len(), 3);
        for x in [&b, &c, &d] {
            let id = x.laptop_id_hex();
            assert_eq!(
                reg.members.contains_key(&id),
                x.laptop_id_hex() != b.laptop_id_hex(),
                "{id}"
            );
        }
        // a fleet that is not ours is refused whole
        assert!(read_registry(&drive, &staging, Some(&[7u8; 32])).is_err());
        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&staging);
    }

    #[test]
    fn a_registry_out_of_order_on_the_drive_is_refused() {
        let root = temp("reg");
        let drive = FolderDrive::new(&root).unwrap();
        let a = laptop();
        let (first, _, _) = create_fleet(&a, "A").unwrap();
        let staging = temp("rs");
        publish(&drive, &staging, &first, 1).unwrap(); // numbered as if it were second
        assert!(read_registry(&drive, &staging, None).is_err());
        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&staging);
    }
}
