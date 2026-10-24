//! The AI layer's place in the shell (docs/SPEC.md, "AI layer"): the Claude API key,
//! the monthly spend cap, and the queue of flights waiting for their analysis pass.
//!
//! Every analysis queues its flight. A worker sends one flight at a time once the
//! start-up self-test has opened the gate and a key is set. A flight whose current
//! analysis already has an answer is not sent again unless a person asks. When the API
//! cannot be reached the flight waits and is tried again later; the deterministic
//! results never wait for it. Past the cap a flight is held until a person asks.
//!
//! The key lives in the OS keychain only. It is read when a request is made, is never
//! written to a file or a log, and is never handed to the interface or the engine.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

use crate::identity::SecretStore;

pub const KEY_NAME: &str = "claude-api-key";
pub const DEFAULT_CAP_USD: f64 = 20.0;
/// Waits before trying an unreachable API again, then the flight is left for a person.
const RETRY: [u64; 4] = [60, 5 * 60, 15 * 60, 60 * 60];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Settings {
    pub monthly_cap_usd: f64,
    /// The language the replies are written in: the interface's.
    pub language: String,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            monthly_cap_usd: DEFAULT_CAP_USD,
            language: "en".into(),
        }
    }
}

fn settings_path(data_dir: &Path) -> PathBuf {
    data_dir.join("ai-settings.json")
}

/// The settings, or the defaults when none were saved or the file does not read.
pub fn load_settings(data_dir: &Path) -> Settings {
    std::fs::read_to_string(settings_path(data_dir))
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

pub fn save_settings(data_dir: &Path, s: &Settings) -> Result<(), String> {
    if !(s.monthly_cap_usd.is_finite() && (0.0..=10_000.0).contains(&s.monthly_cap_usd)) {
        return Err("the monthly cap must be between 0 and 10000 US dollars".into());
    }
    if !["en", "uk"].contains(&s.language.as_str()) {
        return Err("the language must be en or uk".into());
    }
    std::fs::create_dir_all(data_dir).map_err(|e| e.to_string())?;
    let text = serde_json::to_string_pretty(s).map_err(|e| e.to_string())?;
    std::fs::write(settings_path(data_dir), text).map_err(|e| e.to_string())
}

/// Stores a key after a check of its form, so a pasted fragment is caught early.
pub fn set_key(store: &dyn SecretStore, key: &str) -> Result<(), String> {
    let key = key.trim();
    let form_ok = key.starts_with("sk-ant-")
        && (40..=300).contains(&key.len())
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !form_ok {
        return Err("that does not look like a Claude API key (sk-ant-...)".into());
    }
    store.set(KEY_NAME, key.as_bytes())
}

pub fn key(store: &dyn SecretStore) -> Result<Option<Zeroizing<String>>, String> {
    Ok(store
        .get(KEY_NAME)?
        .and_then(|b| String::from_utf8(b.to_vec()).ok())
        .map(Zeroizing::new))
}

#[derive(Debug, Clone, PartialEq)]
pub struct Item {
    pub flight_id: String,
    /// The analysis run it was queued for; None: whatever is current.
    pub run_id: Option<String>,
    /// A person asked: sent even if answered before, and past the cap.
    pub forced: bool,
    pub attempts: u32,
    pub not_before: Instant,
}

/// The flights waiting for their analysis pass, one entry per flight.
#[derive(Default)]
pub struct Queue {
    items: Mutex<VecDeque<Item>>,
    wake: Condvar,
}

impl Queue {
    pub fn push(&self, flight_id: &str, run_id: Option<String>, forced: bool) {
        let mut items = self.items.lock().expect("queue");
        if let Some(it) = items.iter_mut().find(|i| i.flight_id == flight_id) {
            it.forced |= forced;
            it.run_id = run_id.or(it.run_id.take());
            if forced {
                it.not_before = Instant::now();
            }
        } else {
            items.push_back(Item {
                flight_id: flight_id.to_owned(),
                run_id,
                forced,
                attempts: 0,
                not_before: Instant::now(),
            });
        }
        self.wake.notify_all();
    }

    /// Puts an item back after the API could not be reached; false once it has been
    /// tried as often as it will be.
    pub fn retry(&self, mut item: Item) -> bool {
        let Some(wait) = RETRY.get(item.attempts as usize) else {
            return false;
        };
        item.attempts += 1;
        item.not_before = Instant::now() + Duration::from_secs(*wait);
        let mut items = self.items.lock().expect("queue");
        if !items.iter().any(|i| i.flight_id == item.flight_id) {
            items.push_back(item);
        }
        true
    }

    /// The next item that is due, waiting up to `limit` for one.
    pub fn next(&self, limit: Duration) -> Option<Item> {
        let end = Instant::now() + limit;
        let mut items = self.items.lock().expect("queue");
        loop {
            let now = Instant::now();
            if let Some(pos) = items.iter().position(|i| i.not_before <= now) {
                return items.remove(pos);
            }
            if now >= end {
                return None;
            }
            let soonest = items
                .iter()
                .map(|i| i.not_before)
                .min()
                .unwrap_or(end)
                .min(end);
            items = self
                .wake
                .wait_timeout(items, soonest.saturating_duration_since(now))
                .expect("queue")
                .0;
        }
    }

    pub fn len(&self) -> usize {
        self.items.lock().expect("queue").len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;

    #[test]
    fn the_key_is_checked_kept_and_removed() {
        let store = MemoryStore::default();
        assert!(set_key(&store, "hello").is_err());
        assert!(set_key(&store, "sk-ant-short").is_err());
        let good = format!("sk-ant-api03-{}", "a".repeat(80));
        set_key(&store, &format!("  {good}\n")).unwrap();
        assert_eq!(key(&store).unwrap().unwrap().as_str(), good);
        store.delete(KEY_NAME).unwrap();
        assert!(key(&store).unwrap().is_none());
        store.delete(KEY_NAME).unwrap(); // twice is fine
    }

    #[test]
    fn settings_round_trip_and_are_checked() {
        let dir = std::env::temp_dir().join(format!("fi-ai-settings-{}", std::process::id()));
        assert_eq!(load_settings(&dir), Settings::default());
        let s = Settings {
            monthly_cap_usd: 5.5,
            language: "uk".into(),
        };
        save_settings(&dir, &s).unwrap();
        assert_eq!(load_settings(&dir), s);
        assert!(
            save_settings(
                &dir,
                &Settings {
                    monthly_cap_usd: -1.0,
                    ..s.clone()
                }
            )
            .is_err()
        );
        assert!(
            save_settings(
                &dir,
                &Settings {
                    language: "fr".into(),
                    ..s.clone()
                }
            )
            .is_err()
        );
        let text = std::fs::read_to_string(settings_path(&dir)).unwrap();
        assert!(!text.contains("sk-"), "no key in the settings file");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn one_entry_per_flight_and_asking_jumps_the_wait() {
        let q = Queue::default();
        q.push("f1", Some("r1".into()), false);
        q.push("f2", None, false);
        q.push("f1", Some("r2".into()), false);
        assert_eq!(q.len(), 2);
        let first = q.next(Duration::ZERO).unwrap();
        assert_eq!(
            (first.flight_id.as_str(), first.run_id.as_deref()),
            ("f1", Some("r2"))
        );
        assert!(q.retry(first.clone()));
        let second = q.next(Duration::ZERO).unwrap();
        assert_eq!(second.flight_id, "f2");
        assert!(
            q.next(Duration::from_millis(10)).is_none(),
            "f1 waits for its retry"
        );
        q.push("f1", None, true);
        let asked = q.next(Duration::ZERO).unwrap();
        assert!(asked.forced && asked.attempts == 1);
        let mut tired = asked;
        tired.attempts = RETRY.len() as u32;
        assert!(!q.retry(tired));
    }
}
