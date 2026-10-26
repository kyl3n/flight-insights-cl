//! Weather's place in the shell (docs/SPEC.md, "Weather and wind"): the Open-Meteo key,
//! and the downloader that fetches the fleet's tiles on a fixed schedule.
//!
//! The key lives in the OS keychain only, like the Claude key. In a fleet it travels in
//! the service-key set and is entered once. It is read when a request is made, is never
//! written to a file or a log, and is never handed to the interface or the engine.
//!
//! Requests go out only at slot instants (see `plan`): a laptop that is asleep or closed
//! at one waits for the next, rather than catching up when it is opened, so request
//! times follow the schedule and never when someone came back from flying. Nothing is
//! downloaded, space weather included, until an Open-Meteo key is set.

use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use zeroize::Zeroizing;

pub mod fetch;
pub mod plan;

use crate::identity::SecretStore;

pub const KEY_NAME: &str = "open-meteo-key";
/// How late after its instant a slot may still be sent: a laptop woken from sleep just
/// after one sends it, one opened hours later does not.
pub const GRACE_MS: i64 = 15 * 60_000;
/// The longest the worker sleeps between looks at the clock.
pub const WAKE_MS: i64 = 15 * 60_000;

/// Whether text has the form of an Open-Meteo key. Their keys are not documented
/// beyond being sent as the `apikey` query parameter, so only characters that need no
/// escaping in a URL are accepted, which also keeps a pasted fragment of something
/// else out.
pub fn key_form_ok(key: &str) -> bool {
    (8..=200).contains(&key.len())
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

/// Stores a key after a check of its form.
pub fn set_key(store: &dyn SecretStore, key: &str) -> Result<(), String> {
    let key = key.trim();
    if !key_form_ok(key) {
        return Err("that does not look like an Open-Meteo API key".into());
    }
    store.set(KEY_NAME, key.as_bytes())
}

pub fn key(store: &dyn SecretStore) -> Result<Option<Zeroizing<String>>, String> {
    Ok(store
        .get(KEY_NAME)?
        .and_then(|b| String::from_utf8(b.to_vec()).ok())
        .map(Zeroizing::new))
}

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as i64)
}

#[derive(Serialize, Deserialize)]
struct Stored {
    offset_min: u32,
    seed: String,
}

/// This laptop's schedule: drawn at random once and kept, so it holds across starts.
/// It names no place and no time of flying.
pub fn load_schedule(data_dir: &Path) -> plan::Schedule {
    let path = data_dir.join("weather-schedule.json");
    let read = std::fs::read_to_string(&path)
        .ok()
        .and_then(|t| serde_json::from_str::<Stored>(&t).ok())
        .and_then(|s| {
            let mut seed = [0u8; 32];
            hex::decode_to_slice(&s.seed, &mut seed).ok()?;
            Some(plan::Schedule {
                offset_min: s.offset_min % 360,
                seed,
            })
        });
    if let Some(s) = read {
        return s;
    }
    let random = crate::sync::sodium::random::<34>();
    let mut seed = [0u8; 32];
    seed.copy_from_slice(&random[2..]);
    let made = plan::Schedule {
        offset_min: u32::from(u16::from_le_bytes([random[0], random[1]])) % 360,
        seed,
    };
    let stored = Stored {
        offset_min: made.offset_min,
        seed: hex::encode(made.seed),
    };
    let _ = std::fs::create_dir_all(data_dir);
    let _ = std::fs::write(&path, serde_json::to_string(&stored).unwrap_or_default());
    made
}

/// The downloader's state, for the interface: never a tile or a place.
#[derive(Default)]
pub struct Worker {
    pub state: Mutex<Value>,
    last_slot: Mutex<Option<i64>>,
}

impl Worker {
    /// The slot to send now, if `now` is within its grace and it has not been sent.
    pub fn take_slot(&self, schedule: &plan::Schedule, now: i64) -> Option<i64> {
        let slot = schedule.slot_at(now);
        let late = now - schedule.instant(slot);
        let mut last = self.last_slot.lock().expect("weather slot");
        if late > GRACE_MS || *last == Some(slot) {
            return None;
        }
        *last = Some(slot);
        Some(slot)
    }

    pub fn record(&self, outcome: Value, schedule: &plan::Schedule, now: i64) {
        let mut s = outcome;
        s["last_ms"] = json!(now);
        s["next_ms"] = json!(schedule.next_instant(now));
        *self.state.lock().expect("weather state") = s;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;
    use plan::{DAY_MS, SLOT_MS, days};

    #[test]
    fn the_key_is_checked_kept_and_removed() {
        let store = MemoryStore::default();
        assert!(key(&store).unwrap().is_none());
        for bad in [
            "",
            "short",
            "has space in it",
            "a&b=c-d-e-f",
            "ключ-ключ-ключ",
        ] {
            assert!(set_key(&store, bad).is_err(), "{bad}");
        }
        set_key(&store, "  0123456789abcdefABCDEF_-  ").unwrap();
        assert_eq!(
            key(&store).unwrap().as_deref().map(|s| s.as_str()),
            Some("0123456789abcdefABCDEF_-")
        );
        store.delete(KEY_NAME).unwrap();
        assert!(key(&store).unwrap().is_none());
    }

    #[test]
    fn the_schedule_is_drawn_once_and_kept() {
        let dir = std::env::temp_dir().join(format!(
            "fi-weather-schedule-{}",
            hex::encode(crate::sync::sodium::random::<6>().as_ref())
        ));
        let a = load_schedule(&dir);
        let b = load_schedule(&dir);
        assert_eq!((a.offset_min, a.seed), (b.offset_min, b.seed));
        assert!(a.offset_min < 360);
    }

    #[test]
    fn a_slot_is_sent_once_and_only_near_its_instant() {
        let s = plan::Schedule {
            offset_min: 23,
            seed: [1; 32],
        };
        let w = Worker::default();
        let slot = days(2026, 10, 26) * DAY_MS + SLOT_MS;
        let at = s.instant(slot);
        assert_eq!(w.take_slot(&s, at - 1), None, "the slot before, long past");
        assert_eq!(w.take_slot(&s, at), Some(slot));
        assert_eq!(w.take_slot(&s, at + 60_000), None, "already sent");
        // opened two hours after an instant: nothing until the next one
        let w = Worker::default();
        assert_eq!(w.take_slot(&s, at + 2 * 3_600_000), None);
        assert_eq!(
            w.take_slot(&s, at + SLOT_MS + GRACE_MS),
            Some(slot + SLOT_MS)
        );
    }
}
