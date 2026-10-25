//! The sync soak (spec: Roadmap, Phase 1 gate "sync soak test clean"): simulated
//! laptops, each a real engine with its own store, share one folder drive and do a
//! seeded, random mix of work for a number of rounds:
//!
//! - import fixture logs (the same log on two laptops too);
//! - rename a shared ground station (the same field, at the same time);
//! - plan a flight and accept or reject a recommendation;
//! - record an AI result from a canned reply;
//! - crash (the engine is killed and a new one opens the same store), or stay offline;
//! - tamper: a flipped byte, a truncated copy, a copy under a new name;
//! - a laptop enrolls mid-run, and another is revoked with a key rotation.
//!
//! After the run every laptop syncs until nothing moves, and the invariants hold:
//!
//! - every laptop still in the fleet holds the same synced records (the engine's
//!   digest of them);
//! - every conflict kept both values;
//! - no good object was quarantined, and every bad one was, on every other laptop;
//! - nothing on the drive was ever deleted or changed;
//! - the revoked laptop reads nothing written after its revocation, and nothing it
//!   wrote after it is applied anywhere.
//!
//! CI runs a short soak on every change (the defaults); the "Sync soak" workflow runs
//! long ones by hand. `SOAK_SEED`, `SOAK_ROUNDS` and `SOAK_LAPTOPS` set the run, and a
//! failure prints the seed that reproduces it. It needs FI_ENGINE_CMD, as the shell's
//! other real-engine tests do, and is skipped without it.
//!
//! `SOAK_DRIVE=google` runs it on a real Google shared drive instead of a local folder:
//! the "Sync soak on the drive" workflow does so nightly on the test drive, with
//! `FI_GOOGLE_TEST_REFRESH_TOKEN`, `FI_GOOGLE_TEST_DRIVE_ID` and the OAuth client. Each
//! run writes under its own top-level folder, `soak-<run>-<seed>`, and deletes nothing.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use flight_insights_shell::engine::{CALL_TIMEOUT, Engine, EngineCommand};
use flight_insights_shell::identity::{LaptopIdentity, MemoryStore};
use flight_insights_shell::sync::drive::{Drive, FolderDrive};
use flight_insights_shell::sync::google::{GoogleDrive, shared_drives};
use flight_insights_shell::sync::keyring::{
    Device, Keyring, Registry, create_fleet, enroll, revoke,
};
use flight_insights_shell::sync::oauth::{self, Endpoints};
use flight_insights_shell::sync::object::Name;
use flight_insights_shell::sync::run::{Round, Syncer, publish, read_registry};
use flight_insights_shell::sync::sodium::{self, Sha256};
use serde_json::{Value, json};

/// xorshift64*: small, seeded, the same on every machine.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

fn env(name: &str, default: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

struct Laptop {
    name: String,
    id: LaptopIdentity,
    keys: Keyring,
    engine: Option<Engine>,
    data: PathBuf,
    staging: PathBuf,
    imported: BTreeSet<usize>,
    revoked: bool,
}

impl Laptop {
    fn engine(&self) -> &Engine {
        self.engine.as_ref().expect("a running engine")
    }
    fn call(&self, method: &str, params: Value) -> Value {
        self.engine()
            .client
            .call(method, params, CALL_TIMEOUT)
            .unwrap_or_else(|e| panic!("{}: {method}: {e}", self.name))
    }
    fn hex(&self) -> String {
        self.id.laptop_id_hex()
    }
}

struct Soak {
    cmd: EngineCommand,
    knowledge: Option<PathBuf>,
    root: PathBuf,
    drive: Box<dyn Drive>,
    /// The folder a local drive lies in; None on Google, where files are fetched.
    local: Option<PathBuf>,
    /// Each file's size and SHA-256 when first seen, to show nothing ever changes.
    sums: BTreeMap<String, (u64, [u8; 32])>,
    fingerprint: [u8; 32],
    laptops: Vec<Laptop>,
    bad: BTreeSet<String>, // keys of objects that must be refused
    log: Vec<String>,
}

const FIXTURES: [&str; 2] = ["heli-hover.bin", "copter-quad-hover.bin"];

impl Soak {
    fn fixture(n: usize) -> String {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/sitl/4.7.0")
            .join(FIXTURES[n])
            .to_string_lossy()
            .into_owned()
    }

    fn start(&self, lt: &Laptop) -> Engine {
        let (engine, _) = Engine::start(&self.cmd, self.knowledge.as_deref(), |_, _| {}).unwrap();
        engine
            .unlock(&lt.id, &lt.data, Some(&lt.keys.board_key()))
            .unwrap();
        engine
    }

    fn registry(&self) -> Registry {
        read_registry(
            self.drive.as_ref(),
            &self.root.join("registry"),
            Some(&self.fingerprint),
        )
        .unwrap()
    }

    fn new_laptop(&mut self, name: &str, keys: Keyring, id: LaptopIdentity) {
        let data = self.root.join(format!("data-{name}"));
        let staging = self.root.join(format!("staging-{name}"));
        let mut lt = Laptop {
            name: name.into(),
            id,
            keys,
            engine: None,
            data,
            staging,
            imported: BTreeSet::new(),
            revoked: false,
        };
        lt.engine = Some(self.start(&lt));
        self.laptops.push(lt);
    }

    fn round(&self, n: usize, registry: &Registry) -> Round {
        let lt = &self.laptops[n];
        Syncer {
            engine: lt.engine(),
            drive: self.drive.as_ref(),
            identity: &lt.id,
            registry,
            keyring: &lt.keys,
            staging: lt.staging.clone(),
        }
        .round()
        .unwrap_or_else(|e| panic!("{}: a sync round failed: {e}", lt.name))
    }

    fn fetch(&self, path: &str) -> PathBuf {
        match &self.local {
            Some(base) => base.join(path),
            None => {
                let to = self.root.join("fetched.fio");
                self.drive.download(path, &to).unwrap();
                to
            }
        }
    }

    /// Every file on the drive with its SHA-256. A local drive is hashed whole each
    /// time; on Google a file is fetched once and its size watched after, and
    /// `verify_all` fetches everything again at the end.
    fn snapshot(&mut self) -> BTreeMap<String, [u8; 32]> {
        let mut out = BTreeMap::new();
        for e in self.drive.list("").unwrap() {
            let sum = match (self.local.is_some(), self.sums.get(&e.path)) {
                (false, Some((size, sum))) if *size == e.size => *sum,
                (false, Some(_)) => [0; 32], // its size changed
                _ => {
                    let sum = Sha256::of_file(&self.fetch(&e.path)).unwrap();
                    self.sums.entry(e.path.clone()).or_insert((e.size, sum));
                    sum
                }
            };
            out.insert(e.path, sum);
        }
        out
    }

    fn verify_all(&self) {
        for (path, (_, sum)) in &self.sums {
            let now = Sha256::of_file(&self.fetch(path)).unwrap();
            assert_eq!(&now, sum, "{path} changed on the drive");
        }
    }

    // --- the work ---------------------------------------------------------------------

    fn act(&mut self, n: usize, rng: &mut Rng) {
        let choice = rng.below(100);
        let who = self.laptops[n].name.clone();
        match choice {
            0..=24 => {
                let f = rng.below(FIXTURES.len());
                if !self.laptops[n].imported.insert(f) {
                    return;
                }
                let lt = &self.laptops[n];
                let airframes = lt.call("airframe.list", json!({}));
                let airframe = match airframes.as_array().unwrap().first() {
                    Some(a) => a["id"].clone(),
                    None => {
                        let p = lt.call(
                            "platform.create",
                            json!({ "name": "Fleet", "vehicle": "copter" }),
                        );
                        lt.call(
                            "airframe.create",
                            json!({ "platform_id": p["id"], "name": "AF" }),
                        )["id"]
                            .clone()
                    }
                };
                let scanned = lt.call("import.scan", json!({ "paths": [Self::fixture(f)] }));
                lt.call(
                    "import.commit",
                    json!({ "batch": scanned["batch"], "decisions": [{ "log": "f1", "airframe_id": airframe }] }),
                );
                self.log.push(format!("{who} imports {}", FIXTURES[f]));
            }
            25..=44 => {
                let lt = &self.laptops[n];
                let stations = lt.call("station.list", json!({}));
                let Some(s) = stations.as_array().unwrap().first() else {
                    return;
                };
                let name = format!("GCS {}", rng.below(1000));
                lt.call(
                    "station.rename",
                    json!({ "id": s["id"], "version": s["version"], "name": name }),
                );
                self.log.push(format!("{who} renames the station"));
            }
            45..=59 => {
                let lt = &self.laptops[n];
                let flights = lt.call("flight.list", json!({}));
                let Some(f) = flights.as_array().unwrap().first() else {
                    return;
                };
                let plan = lt.call("recommend.plan", json!({ "flight_id": f["id"] }));
                let open = plan["recommendations"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|r| r["record_status"] == "proposed")
                    .cloned();
                if let Some(r) = open {
                    let status = if rng.below(2) == 0 {
                        "accepted"
                    } else {
                        "rejected"
                    };
                    let _ = lt.engine().client.call(
                        "recommend.set_status",
                        json!({ "id": r["id"], "status": status, "version": r["record_version"] }),
                        CALL_TIMEOUT,
                    );
                    self.log
                        .push(format!("{who} marks a recommendation {status}"));
                }
            }
            60..=69 => {
                let lt = &self.laptops[n];
                let flights = lt.call("flight.list", json!({}));
                let Some(f) = flights.as_array().unwrap().first() else {
                    return;
                };
                let prepared = lt.call("ai.prepare", json!({ "flight_id": f["id"] }));
                if prepared["engine"]["ok"] != true {
                    return;
                }
                let pid = prepared["packet_id"].clone();
                lt.call(
                    "ai.record",
                    json!({ "packet_id": pid, "shell_reasons": [], "sent": true }),
                );
                let reply = json!({ "summary": "Soak reply.", "root_causes": [], "recommendations": [],
                                    "disagreements": [], "questions": [], "tests_to_fly": [] });
                lt.call(
                    "ai.accept",
                    json!({ "packet_id": pid, "model": "soak", "stop_reason": "end_turn",
                                             "reply": reply.to_string() }),
                );
                self.log.push(format!("{who} records an AI result"));
            }
            70..=77 => {
                let lt = &mut self.laptops[n];
                drop(lt.engine.take()); // killed, not shut down
                let engine = {
                    let lt = &self.laptops[n];
                    self.start(lt)
                };
                self.laptops[n].engine = Some(engine);
                self.log.push(format!("{who} crashes and restarts"));
            }
            78..=85 => self.tamper(n, rng),
            _ => {}
        }
    }

    /// A bad object in another laptop's folder: a flipped byte, a cut-off copy, or
    /// the same bytes under a new name. Each reuses a sequence number that is there.
    fn tamper(&mut self, n: usize, rng: &mut Rng) {
        let me = self.laptops[n].hex();
        let objects: Vec<_> = self
            .drive
            .list("devices/")
            .unwrap()
            .into_iter()
            .filter(|e| !e.path.contains(&me))
            .collect();
        if objects.is_empty() {
            return;
        }
        let victim = &objects[rng.below(objects.len())];
        let mut bytes = fs::read(self.fetch(&victim.path)).unwrap();
        let how = rng.below(3);
        match how {
            0 => {
                let i = rng.below(bytes.len());
                bytes[i] ^= 0x10;
            }
            1 => bytes.truncate(bytes.len() / 2),
            _ => {}
        }
        let forged = Name {
            object_key: *sodium::random::<32>(),
            ..Name::parse(&victim.path).unwrap()
        };
        let staged = self.root.join("forged.fio");
        fs::write(&staged, &bytes).unwrap();
        self.drive.upload(&forged.path(), &staged).unwrap();
        self.bad.insert(hex::encode(forged.object_key));
        self.log.push(format!(
            "{} plants a {} object",
            self.laptops[n].name,
            ["flipped", "cut", "renamed"][how]
        ));
    }

    fn settle(&self, registry: &Registry) {
        for _ in 0..10 {
            let mut moved = false;
            for n in 0..self.laptops.len() {
                let r = self.round(n, registry);
                moved |= r.uploaded > 0 || r.applied > 0;
            }
            if !moved {
                return;
            }
        }
        panic!("sync did not settle in ten passes");
    }
}

/// A local folder, or the test shared drive with `SOAK_DRIVE=google`.
fn the_drive(root: &Path, seed: u64) -> (Box<dyn Drive>, Option<PathBuf>) {
    if std::env::var("SOAK_DRIVE").as_deref() != Ok("google") {
        let local = root.join("drive");
        return (Box::new(FolderDrive::new(&local).unwrap()), Some(local));
    }
    // A pasted secret may carry a line break, or the drive's name after its id as the
    // sign-in helper lists them; no id or token holds whitespace.
    let need = |name: &str| {
        std::env::var(name)
            .ok()
            .and_then(|v| v.split_whitespace().next().map(String::from))
            .unwrap_or_else(|| panic!("SOAK_DRIVE=google needs {name}"))
    };
    let client = oauth::built_in_client().unwrap_or_else(|| oauth::Client {
        id: need("FI_GOOGLE_CLIENT_ID"),
        secret: std::env::var("FI_GOOGLE_CLIENT_SECRET").unwrap_or_default(),
    });
    let run = std::env::var("SOAK_RUN").unwrap_or_else(|_| std::process::id().to_string());
    let folder = format!("soak-{run}-{seed}");
    eprintln!("sync soak: on the test shared drive, under {folder}/");
    let drive_id = need("FI_GOOGLE_TEST_DRIVE_ID");
    let refresh = zeroize::Zeroizing::new(need("FI_GOOGLE_TEST_REFRESH_TOKEN"));
    // Only the count is printed: the drives' names are the company's, not the soak's.
    let visible = shared_drives(&Endpoints::default(), &client, &refresh)
        .unwrap_or_else(|e| panic!("the test account's shared drives could not be listed: {e}"));
    assert!(
        visible.iter().any(|(id, _)| *id == drive_id),
        "FI_GOOGLE_TEST_DRIVE_ID is none of the {} shared drive(s) the test account is a member \
         of: copy the test drive's id from the sign-in helper's list (docs/google-setup.md, step 6)",
        visible.len()
    );
    let drive = GoogleDrive::new(Endpoints::default(), client, &drive_id, refresh)
        .and_then(|d| d.under(&folder))
        .unwrap();
    (Box::new(drive), None)
}

#[test]
fn soak() {
    if std::env::var("FI_ENGINE_CMD").is_err() {
        eprintln!("FI_ENGINE_CMD not set: skipped");
        return;
    }
    let seed = env("SOAK_SEED", 1);
    let rounds = env("SOAK_ROUNDS", 10) as usize;
    let count = env("SOAK_LAPTOPS", 3).max(2) as usize;
    eprintln!("sync soak: SOAK_SEED={seed} SOAK_ROUNDS={rounds} SOAK_LAPTOPS={count}");
    let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);
    let root = std::env::temp_dir().join(format!("fi-soak-{seed}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);

    // the fleet, and its first laptops
    let ids: Vec<LaptopIdentity> = (0..count)
        .map(|_| {
            LaptopIdentity::load_or_create(&MemoryStore::default())
                .unwrap()
                .0
        })
        .collect();
    let (drive, local) = the_drive(&root, seed);
    let (first, keys0, _recovery) = create_fleet(&ids[0], "L0").unwrap();
    publish(drive.as_ref(), &root.join("registry"), &first, 0).unwrap();
    let mut registry = read_registry(drive.as_ref(), &root.join("registry"), None).unwrap();
    for (n, id) in ids.iter().enumerate().skip(1) {
        let e = enroll(&registry, &keys0, &ids[0], Device::of(id, &format!("L{n}"))).unwrap();
        publish(drive.as_ref(), &root.join("registry"), &e, n as u64).unwrap();
        registry = read_registry(drive.as_ref(), &root.join("registry"), None).unwrap();
    }
    let mut soak = Soak {
        cmd: EngineCommand::resolve(Path::new(".")).unwrap(),
        knowledge: std::env::var_os("FI_KNOWLEDGE_DIR").map(PathBuf::from),
        root: root.clone(),
        drive,
        local,
        sums: BTreeMap::new(),
        fingerprint: registry.fingerprint,
        laptops: Vec::new(),
        bad: BTreeSet::new(),
        log: Vec::new(),
    };
    for (n, id) in ids.into_iter().enumerate() {
        let keys = Keyring::from_registry(&registry, &id).unwrap();
        soak.new_laptop(&format!("L{n}"), keys, id);
    }
    soak.laptops[0].call("station.create", json!({ "name": "GCS" }));

    let enroll_at = rounds / 3;
    let revoke_at = 2 * rounds / 3;
    let mut revoked_after: Option<(String, u64)> = None;
    let mut at_revocation: BTreeSet<String> = BTreeSet::new();
    let mut before = soak.snapshot();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        for round in 0..rounds {
            registry = soak.registry();
            if round == enroll_at {
                let id = LaptopIdentity::load_or_create(&MemoryStore::default())
                    .unwrap()
                    .0;
                let name = format!("L{}", soak.laptops.len());
                let e = enroll(
                    &registry,
                    &soak.laptops[0].keys,
                    &soak.laptops[0].id,
                    Device::of(&id, &name),
                )
                .unwrap();
                publish(
                    soak.drive.as_ref(),
                    &root.join("registry"),
                    &e,
                    registry.events.len() as u64,
                )
                .unwrap();
                registry = soak.registry();
                let keys = Keyring::from_registry(&registry, &id).unwrap();
                soak.new_laptop(&name, keys, id);
                soak.log.push(format!("{name} enrolls"));
            }
            if round == revoke_at {
                soak.settle(&registry);
                let gone = soak.laptops[1].hex();
                let status = soak.laptops[0].call("sync.status", json!({}));
                let last = status["laptops"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|l| l["laptop"] == gone.as_str())
                    .map_or(0, |l| l["latest"].as_u64().unwrap());
                let (e, rotated) = revoke(
                    &registry,
                    &soak.laptops[0].keys,
                    &soak.laptops[0].id,
                    &gone,
                    last,
                )
                .unwrap();
                publish(
                    soak.drive.as_ref(),
                    &root.join("registry"),
                    &e,
                    registry.events.len() as u64,
                )
                .unwrap();
                registry = soak.registry();
                soak.laptops[0].keys = rotated;
                for lt in soak.laptops.iter_mut().skip(2) {
                    lt.keys = Keyring::from_registry(&registry, &lt.id).unwrap();
                }
                soak.laptops[1].revoked = true;
                at_revocation = soak.snapshot().into_keys().collect();
                revoked_after = Some((gone, last));
                soak.log
                    .push(format!("L1 is revoked after its sequence {last}"));
            }
            for n in 0..soak.laptops.len() {
                soak.act(n, &mut rng);
                if rng.below(5) == 0 {
                    continue; // offline this round
                }
                soak.round(n, &registry);
            }
            // nothing on the drive is ever deleted or changed
            let now = soak.snapshot();
            for (path, sum) in &before {
                assert_eq!(now.get(path), Some(sum), "{path} changed or went");
            }
            before = now;
        }

        registry = soak.registry();
        soak.settle(&registry);
        soak.verify_all();
        // what the revoked laptop wrote after its revocation must be refused
        let mut late = BTreeSet::new();
        if let Some((gone, last)) = &revoked_after {
            for e in soak.drive.list(&format!("devices/{gone}/")).unwrap() {
                let name = Name::parse(&e.path).unwrap();
                if name.sequence > *last && !soak.bad.contains(&hex::encode(name.object_key)) {
                    late.insert(hex::encode(name.object_key));
                }
            }
        }
        let active: Vec<&Laptop> = soak.laptops.iter().filter(|l| !l.revoked).collect();
        let digest = active[0].call("sync.digest", json!({}));
        for lt in &active {
            let theirs = lt.call("sync.digest", json!({}));
            if theirs["sha256"] != digest["sha256"] {
                let differ: Vec<String> = digest["tables"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .filter(|(t, v)| theirs["tables"][t.as_str()] != **v)
                    .map(|(t, v)| {
                        format!(
                            "{t}: {} vs {}",
                            v["rows"],
                            theirs["tables"][t.as_str()]["rows"]
                        )
                    })
                    .collect();
                let tables: Vec<&str> = differ
                    .iter()
                    .map(|d| d.split(':').next().unwrap())
                    .collect();
                let ours = active[0].call("sync.digest", json!({ "show": tables }));
                let them = lt.call("sync.digest", json!({ "show": tables }));
                for t in &tables {
                    let rows = |v: &Value| -> BTreeSet<String> {
                        // shown without the long free-text fields
                        v["tables"][*t]["data"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(|r| {
                                let mut row: Value =
                                    serde_json::from_str(r.as_str().unwrap()).unwrap();
                                if let Some(o) = row.as_object_mut() {
                                    o.retain(|k, _| !k.ends_with("_json"));
                                }
                                row.to_string()
                            })
                            .collect()
                    };
                    let (a, b) = (rows(&ours), rows(&them));
                    for r in a.difference(&b).take(3) {
                        eprintln!(
                            "  {t} only on {}: {}",
                            active[0].name,
                            &r[..r.len().min(700)]
                        );
                    }
                    for r in b.difference(&a).take(3) {
                        eprintln!("  {t} only on {}: {}", lt.name, &r[..r.len().min(700)]);
                    }
                }
                panic!(
                    "{} holds other records than {}: {differ:?}",
                    lt.name, active[0].name
                );
            }
            for c in lt
                .call("sync.conflicts", json!({ "include_resolved": true }))
                .as_array()
                .unwrap()
            {
                assert!(
                    c.get("kept").is_some()
                        && c.get("other").is_some()
                        && c["kept_hlc"] != c["other_hlc"],
                    "{c}"
                );
            }
            let status = lt.call("sync.status", json!({}));
            let held: BTreeSet<String> = status["quarantine"]
                .as_array()
                .unwrap()
                .iter()
                .map(|q| q["object_key"].as_str().unwrap().to_string())
                .collect();
            for key in &held {
                assert!(
                    soak.bad.contains(key) || late.contains(key),
                    "{}: a good object {key} was quarantined",
                    lt.name
                );
            }
            let mine = lt.hex();
            for e in soak.drive.list("devices/").unwrap() {
                let name = Name::parse(&e.path).unwrap();
                let key = hex::encode(name.object_key);
                if !e.path.contains(&mine) && (soak.bad.contains(&key) || late.contains(&key)) {
                    assert!(
                        held.contains(&key),
                        "{}: a bad object {key} was not quarantined",
                        lt.name
                    );
                }
            }
        }
        // the revoked laptop cannot read what the others wrote after its revocation
        if let Some((gone, _)) = &revoked_after {
            let n = soak.laptops.iter().position(|l| l.revoked).unwrap();
            soak.round(n, &registry);
            let status = soak.laptops[n].call("sync.status", json!({}));
            let refused: BTreeMap<String, String> = status["quarantine"]
                .as_array()
                .unwrap()
                .iter()
                .map(|q| {
                    (
                        q["object_key"].as_str().unwrap().into(),
                        q["reason"].as_str().unwrap().into(),
                    )
                })
                .collect();
            let unknown = soak.laptops[n].call("sync.unknown", json!({ "object_keys": [] }));
            assert_eq!(unknown, json!([]));
            for e in soak.drive.list("devices/").unwrap() {
                let key = hex::encode(Name::parse(&e.path).unwrap().object_key);
                if at_revocation.contains(&e.path)
                    || e.path.contains(gone.as_str())
                    || soak.bad.contains(&key)
                {
                    continue;
                }
                assert_eq!(
                    refused.get(&key).map(String::as_str),
                    Some("written with a fleet key this laptop does not hold"),
                    "the revoked laptop read {}",
                    e.path
                );
            }
        }
    }));
    if let Err(e) = result {
        eprintln!(
            "sync soak failed: rerun with SOAK_SEED={seed} SOAK_ROUNDS={rounds} SOAK_LAPTOPS={count}"
        );
        for line in soak.log.iter().rev().take(40).rev() {
            eprintln!("  {line}");
        }
        std::panic::resume_unwind(e);
    }
    eprintln!(
        "sync soak clean: {} actions, {} bad objects refused",
        soak.log.len(),
        soak.bad.len()
    );
    for lt in &soak.laptops {
        lt.engine().stop();
    }
    let _ = fs::remove_dir_all(&root);
}
