//! Weather fetched once for the whole fleet (docs/SPEC.md, "Weather and wind", "Fetched
//! once"): each download is shared through the drive as an object like any other,
//! encrypted with the fleet key and signed, and a laptop looks there before asking the
//! provider.
//!
//! ```text
//! weather/<folder>/<laptop-id>/<sequence>-<object-key>.fio
//! ```
//!
//! The folder is a keyed hash of what the download is (source, tile and period) under
//! the fleet's object-name key, so Google learns neither the tile nor the period, and
//! cannot test a guess. Inside, the plaintext names them again and is checked against
//! the folder before it is kept. The sequence is the writer's clock in seconds: a laptop
//! removed from the fleet has every weather object refused, as later than its last
//! allowed sequence, so another laptop fetches that download again.
//!
//! In a fleet the schedule (the offset minute, and the seed for new tiles' delays) comes
//! from the fleet key, so every laptop agrees on it, and the laptops take turns: each
//! slot ranks them by a keyed hash, and each waits 3 minutes a place before starting, up
//! to 12, so the first to start fetches and the rest find its downloads.

use std::fs::{self, File};
use std::io::{BufWriter, Cursor};
use std::path::{Path, PathBuf};

use serde_json::json;

use crate::claude::Engine;
use crate::identity::LaptopIdentity;
use crate::sync::drive::Drive;
use crate::sync::keyring::{Keyring, Reader, Registry};
use crate::sync::object::{self, Name};
use crate::sync::sodium::{self, Hmac};
use crate::weather::fetch::Share;
use crate::weather::plan::{Request, Schedule};

pub const FOLDER: &str = "weather";
const FORMAT: &str = "fi.weather/1";
/// Each place in a slot's turn order waits this long.
const TURN_MS: i64 = 3 * 60_000;
const LAST_TURN: usize = 4;

/// The fleet's schedule: the same on every laptop holding the fleet key.
pub fn fleet_schedule(object_keys: &[u8; 32]) -> Schedule {
    let derive = |label: &[u8]| {
        let mut mac = Hmac::new(object_keys);
        mac.update(label);
        mac.finish()
    };
    let offset = derive(b"fi.weather.offset");
    Schedule {
        offset_min: u32::from(u16::from_le_bytes([offset[0], offset[1]])) % 360,
        seed: derive(b"fi.weather.seed"),
    }
}

/// How long this laptop waits into a slot before starting: its place in the slot's turn
/// order among the fleet's laptops still in it, 3 minutes a place, at most 12.
pub fn turn_wait_ms(object_keys: &[u8; 32], slot: i64, me: &str, members: &[String]) -> i64 {
    let order = |laptop: &str| {
        let mut mac = Hmac::new(object_keys);
        mac.update(b"fi.weather.turn");
        mac.update(&slot.to_le_bytes());
        mac.update(laptop.as_bytes());
        mac.finish()
    };
    let mine = order(me);
    let ahead = members
        .iter()
        .filter(|m| m.as_str() != me && order(m) < mine)
        .count();
    ahead.min(LAST_TURN) as i64 * TURN_MS
}

fn label(r: &Request) -> String {
    format!("{FORMAT}\n{}\n{}\n{}\n", r.source.name(), r.tile, r.period)
}

/// The drive folder for one download: a keyed hash of what it is.
pub fn folder(object_keys: &[u8; 32], r: &Request) -> String {
    let mut mac = Hmac::new(object_keys);
    mac.update(label(r).as_bytes());
    format!("{FOLDER}/{}", hex::encode(&mac.finish()[..16]))
}

/// A weather object's name, from its path under its folder.
fn name_in(folder: &str, path: &str) -> Option<Name> {
    let rest = path.strip_prefix(folder)?.strip_prefix('/')?;
    Name::parse_in("x", &format!("x/{rest}"))
}

/// Sharing through the fleet's drive.
pub struct Fleet<'a> {
    pub drive: &'a dyn Drive,
    pub registry: &'a Registry,
    pub keyring: &'a Keyring,
    pub identity: &'a LaptopIdentity,
    pub staging: PathBuf,
}

impl Fleet<'_> {
    fn staged(&self, what: &str) -> Result<PathBuf, String> {
        fs::create_dir_all(&self.staging).map_err(|e| e.to_string())?;
        Ok(self.staging.join(format!(
            "{what}-{}",
            hex::encode(sodium::random::<8>().as_ref())
        )))
    }

    /// The download's plaintext from one object, or None when it does not hold.
    fn open(&self, path: &str, name: &Name, r: &Request) -> Result<Option<Vec<u8>>, String> {
        let local = self.staged("weather-in")?;
        self.drive
            .download(path, &local)
            .map_err(|e| e.to_string())?;
        let mut plain = Vec::new();
        let read = object::read(
            &local,
            name,
            &Reader::new(self.registry, self.keyring),
            &mut plain,
        );
        let _ = fs::remove_file(&local);
        if read.is_err() {
            return Ok(None); // not the fleet's, or tampered with: never kept
        }
        let head = label(r);
        Ok(plain.strip_prefix(head.as_bytes()).map(|gz| gz.to_vec()))
    }
}

impl Share for Fleet<'_> {
    fn take(&self, engine: &dyn Engine, r: &Request) -> Result<bool, String> {
        let folder = folder(&self.keyring.object_keys(), r);
        let mut found: Vec<(Name, String)> = self
            .drive
            .list(&format!("{folder}/"))
            .map_err(|e| e.to_string())?
            .into_iter()
            .filter_map(|e| name_in(&folder, &e.path).map(|n| (n, e.path)))
            .collect();
        found.sort_by(|a, b| (b.0.sequence, &b.1).cmp(&(a.0.sequence, &a.1)));
        for (name, path) in found {
            let Some(gz) = self.open(&path, &name, r)? else {
                continue;
            };
            let local = self.staged("weather-gz")?;
            fs::write(&local, &gz).map_err(|e| e.to_string())?;
            let kept = engine.call(
                "weather.store",
                json!({
                    "source": r.source.name(), "tile": r.tile, "period": r.period,
                    "path": local.to_string_lossy(), "gzip": true,
                }),
            );
            let _ = fs::remove_file(&local);
            if kept.is_ok() {
                return Ok(true);
            }
            // a body the engine refuses is passed over like a bad object
        }
        Ok(false)
    }

    fn give(&self, engine: &dyn Engine, r: &Request) -> Result<(), String> {
        let gz_path = self.staged("weather-out")?;
        engine.call(
            "weather.export",
            json!({
                "source": r.source.name(), "tile": r.tile, "period": r.period,
                "path": gz_path.to_string_lossy(),
            }),
        )?;
        let gz = fs::read(&gz_path).map_err(|e| e.to_string());
        let _ = fs::remove_file(&gz_path);
        let mut plain = label(r).into_bytes();
        plain.extend_from_slice(&gz?);
        let mut laptop = [0u8; 16];
        hex::decode_to_slice(self.identity.laptop_id_hex(), &mut laptop)
            .map_err(|e| e.to_string())?;
        let sequence = (crate::weather::now_ms().max(0) / 1000) as u64;
        let local = self.staged("weather-fio")?;
        let written = (|| {
            let mut file = BufWriter::new(File::create(&local)?);
            object::write(
                &mut file,
                &mut Cursor::new(plain.as_slice()),
                laptop,
                sequence,
                &self.keyring.current(),
                &self.keyring.object_keys(),
                &|m| self.identity.sign(m),
            )
        })();
        let result = written.map_err(|e| e.to_string()).and_then(|name| {
            let path = name.path_in(&folder(&self.keyring.object_keys(), r));
            let uploaded = self
                .drive
                .upload(&path, &local)
                .map_err(|e| e.to_string())?;
            let sent = fs::read(&local)
                .map(|b| sodium::sha256(&b))
                .map_err(|e| e.to_string())?;
            if uploaded != sent {
                return Err("the drive's copy of a weather download does not match".into());
            }
            Ok(())
        });
        let _ = fs::remove_file(&local);
        result
    }
}

/// Where a laptop stages weather objects.
pub fn staging(data_dir: &Path) -> PathBuf {
    data_dir.join("weather-staging")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    use crate::identity::MemoryStore;
    use crate::net;
    use crate::sync::app;
    use crate::sync::drive::FolderDrive;
    use crate::sync::run::read_registry;
    use crate::weather::fetch::tests::{FakeEngine, KEY, fake_weather, hosts};
    use crate::weather::fetch::{Round, round};
    use crate::weather::plan::{DAY_MS, Schedule, Source, days};

    fn temp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "fi-weather-share-{name}-{}",
            hex::encode(sodium::random::<6>().as_ref())
        ))
    }

    fn laptop() -> LaptopIdentity {
        LaptopIdentity::load_or_create(&MemoryStore::default())
            .unwrap()
            .0
    }

    struct Two {
        root: PathBuf,
        drive: FolderDrive,
        registry: Registry,
        a: (LaptopIdentity, Keyring),
        b: (LaptopIdentity, Keyring),
    }

    /// A fleet of two laptops on a folder drive.
    fn fleet() -> Two {
        let root = temp("drive");
        let drive = FolderDrive::new(&root).unwrap();
        let staging = temp("staging");
        let (a, b) = (laptop(), laptop());
        let made = app::create(&drive, &staging, &a, "Bench").unwrap();
        let (code, _) = app::code(&b, "Field");
        app::approve(
            &drive,
            &staging,
            &made.fingerprint,
            &made.keyring,
            &a,
            &code,
        )
        .unwrap();
        let registry = read_registry(&drive, &staging, Some(&made.fingerprint)).unwrap();
        let keys_b = app::join(&drive, &staging, &b, &made.fingerprint).unwrap();
        Two {
            root,
            drive,
            registry,
            a: (a, made.keyring),
            b: (b, keys_b),
        }
    }

    fn share<'a>(t: &'a Two, who: &'a (LaptopIdentity, Keyring)) -> Fleet<'a> {
        Fleet {
            drive: &t.drive,
            registry: &t.registry,
            keyring: &who.1,
            identity: &who.0,
            staging: temp("share"),
        }
    }

    fn engine() -> FakeEngine {
        FakeEngine {
            tiles: vec![("N50E030".into(), 0)],
            ..Default::default()
        }
    }

    fn slot() -> i64 {
        days(2026, 10, 24) * DAY_MS
    }

    fn one(mut entries: Vec<crate::sync::drive::Entry>) -> crate::sync::drive::Entry {
        assert_eq!(entries.len(), 1, "one object");
        entries.remove(0)
    }

    fn run(engine: &FakeEngine, url: &str, schedule: &Schedule, share: &Fleet<'_>) -> Round {
        let http = net::client(Duration::from_secs(10)).unwrap();
        round(
            engine,
            &http,
            &hosts(url),
            KEY,
            schedule,
            slot(),
            &temp("staging"),
            &mut |_| {},
            Some(share),
        )
        .unwrap()
    }

    #[test]
    fn each_download_is_fetched_once_for_the_fleet() {
        let t = fleet();
        let schedule = fleet_schedule(&t.a.1.object_keys());
        let fake = fake_weather();
        let (engine_a, engine_b) = (engine(), engine());
        let a = run(&engine_a, &fake.url, &schedule, &share(&t, &t.a));
        assert!(a.kept > 20 && a.given == a.kept && a.taken == 0);
        let asked = fake.seen.lock().unwrap().len();
        assert_eq!(asked, a.kept);
        // B finds every download A shared, and asks the provider for nothing
        let b = run(&engine_b, &fake.url, &schedule, &share(&t, &t.b));
        assert_eq!((b.kept, b.taken, b.given), (a.kept, a.kept, 0));
        assert_eq!(fake.seen.lock().unwrap().len(), asked);
        assert_eq!(
            *engine_b.kept.lock().unwrap(),
            *engine_a.kept.lock().unwrap()
        );
    }

    #[test]
    fn the_drive_learns_neither_tile_nor_period() {
        let t = fleet();
        let schedule = fleet_schedule(&t.a.1.object_keys());
        let fake = fake_weather();
        run(&engine(), &fake.url, &schedule, &share(&t, &t.a));
        let entries = t.drive.list("weather/").unwrap();
        assert!(!entries.is_empty());
        // keyed hashes and the laptop's own sequence number, and nothing else: a substring
        // search alone would find "2026" inside a hash now and then
        let shape =
            regex::Regex::new(r"^weather/[0-9a-f]{32}/[0-9a-f]{32}/[0-9]{12}-[0-9a-f]{64}\.fio$")
                .unwrap();
        for e in &entries {
            assert!(
                shape.is_match(&e.path),
                "{} is not hashes and a sequence",
                e.path
            );
            for word in ["N50E030", "forecast", "reanalysis", "kp"] {
                assert!(!e.path.contains(word), "{} names {word}", e.path);
            }
            let bytes = fs::read(t.root.join(&e.path)).unwrap();
            for word in ["N50E030", "fi.weather", "latitude"] {
                assert!(
                    !bytes.windows(word.len()).any(|w| w == word.as_bytes()),
                    "{word} in the clear"
                );
            }
        }
    }

    #[test]
    fn what_does_not_hold_is_fetched_again() {
        let t = fleet();
        let keys = t.a.1.object_keys();
        let schedule = fleet_schedule(&keys);
        let fake = fake_weather();
        let engine_a = engine();
        run(&engine_a, &fake.url, &schedule, &share(&t, &t.a));
        let asked = fake.seen.lock().unwrap().len();
        let kp = Request {
            source: Source::Kp,
            tile: String::new(),
            period: crate::weather::plan::hour_label(slot()),
            query: Vec::new(),
        };
        // tamper with A's space weather object
        let entry = one(t.drive.list(&format!("{}/", folder(&keys, &kp))).unwrap());
        let file = t.root.join(&entry.path);
        let mut bytes = fs::read(&file).unwrap();
        let mid = bytes.len() / 2;
        bytes[mid] ^= 1;
        let mut perms = fs::metadata(&file).unwrap().permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        perms.set_readonly(false);
        fs::set_permissions(&file, perms).unwrap();
        fs::write(&file, bytes).unwrap();
        // and file a forecast object of A's under another download's folder
        let forecast = engine_a
            .kept
            .lock()
            .unwrap()
            .keys()
            .find(|k| k.0 == "forecast")
            .cloned()
            .unwrap();
        let theirs = Request {
            source: Source::Forecast,
            tile: forecast.1.clone(),
            period: forecast.2.clone(),
            query: Vec::new(),
        };
        let entry = one(t
            .drive
            .list(&format!("{}/", folder(&keys, &theirs)))
            .unwrap());
        let moved = Request {
            period: "2026-10-26T18".into(),
            ..theirs.clone()
        };
        let name = entry.path.rsplit('/').next().unwrap();
        let laptop = entry.path.split('/').nth(2).unwrap();
        t.drive
            .upload(
                &format!("{}/{laptop}/{name}", folder(&keys, &moved)),
                &t.root.join(&entry.path),
            )
            .unwrap();
        let b = share(&t, &t.b);
        let engine_b = engine();
        assert!(!b.take(&engine_b, &kp).unwrap(), "tampered");
        assert!(
            !b.take(&engine_b, &moved).unwrap(),
            "names another download"
        );
        assert!(b.take(&engine_b, &theirs).unwrap());
        // a laptop from another fleet: its object, copied into this fleet's folder, is
        // refused (it writes into its own fleet's folders, which this one never reads)
        let other = fleet();
        let stranger = Fleet {
            drive: &t.drive,
            ..share(&other, &other.a)
        };
        engine_b.kept.lock().unwrap().insert(
            ("kp".into(), String::new(), kp.period.clone()),
            b"[]".to_vec(),
        );
        stranger.give(&engine_b, &kp).unwrap();
        let theirs_folder = folder(&other.a.1.object_keys(), &kp);
        assert_ne!(theirs_folder, folder(&keys, &kp));
        let placed = one(t.drive.list(&format!("{theirs_folder}/")).unwrap());
        let rest = placed.path.strip_prefix(&theirs_folder).unwrap();
        t.drive
            .upload(
                &format!("{}{rest}", folder(&keys, &kp)),
                &t.root.join(&placed.path),
            )
            .unwrap();
        let engine_b = engine();
        assert!(!b.take(&engine_b, &kp).unwrap(), "another fleet's");
        // B's round fetches the space weather itself
        let round_b = run(&engine(), &fake.url, &schedule, &b);
        assert!(round_b.given >= 1);
        assert!(fake.seen.lock().unwrap().len() > asked);
    }

    #[test]
    fn the_fleet_agrees_on_its_schedule_and_takes_turns() {
        let t = fleet();
        let (ka, kb) = (t.a.1.object_keys(), t.b.1.object_keys());
        let (sa, sb) = (fleet_schedule(&ka), fleet_schedule(&kb));
        assert_eq!((sa.offset_min, sa.seed), (sb.offset_min, sb.seed));
        let members = vec![t.a.0.laptop_id_hex(), t.b.0.laptop_id_hex()];
        let mut waits: Vec<i64> = members
            .iter()
            .map(|m| turn_wait_ms(&ka, slot(), m, &members))
            .collect();
        waits.sort();
        assert_eq!(waits, [0, TURN_MS]);
        // the order changes from slot to slot, so no laptop always fetches
        let first = |s: i64| turn_wait_ms(&ka, s, &members[0], &members) == 0;
        let leads = (0..40)
            .filter(|k| first(slot() + k * 6 * 3_600_000))
            .count();
        assert!((5..35).contains(&leads), "{leads}");
        let many: Vec<String> = (0..8).map(|i| format!("{i:032x}")).collect();
        assert!(
            many.iter()
                .all(|m| turn_wait_ms(&ka, slot(), m, &many) <= 12 * 60_000)
        );
    }
}
