//! Weather's place in the shell (docs/SPEC.md, "Weather and wind"): the Open-Meteo key.
//!
//! The key lives in the OS keychain only, like the Claude key. In a fleet it travels in
//! the service-key set and is entered once. It is read when a request is made, is never
//! written to a file or a log, and is never handed to the interface or the engine.

use zeroize::Zeroizing;

use crate::identity::SecretStore;

pub const KEY_NAME: &str = "open-meteo-key";

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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::MemoryStore;

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
}
