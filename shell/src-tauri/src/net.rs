//! The shell's only way onto the network (docs/SPEC.md, "Location and privacy"; the
//! engine makes no network requests at all).
//!
//! Every HTTP client is built here, and every request's address is checked here first:
//! HTTPS only, to a host on the allowlist, with no redirects. The allowlist grows one
//! endpoint at a time as features that need one arrive: the Claude API, the Google
//! Drive API, the Open-Meteo API and NOAA SWPC now; the update server with its own work.

use std::sync::Once;
use std::time::Duration;

pub use reqwest::blocking::{Client, RequestBuilder, Response};
pub use reqwest::{StatusCode, Url};

/// Hosts the app may talk to.
pub const ALLOWED_HOSTS: &[&str] = &[
    "api.anthropic.com",
    // the Google Drive API: sign-in tokens, and the Drive API with its uploads
    "oauth2.googleapis.com",
    "www.googleapis.com",
    // the Open-Meteo commercial API: forecasts, and ERA5 reanalysis (customer- hosts take
    // the key); whole regional tiles only, never a flight's position
    "customer-api.open-meteo.com",
    "customer-archive-api.open-meteo.com",
    // NOAA's Space Weather Prediction Center: the planetary K-index, asked with no
    // parameters at all
    "services.swpc.noaa.gov",
];

static TLS: Once = Once::new();

/// The client every request goes through: HTTPS only, no redirects, certificates
/// checked against the operating system's trust store.
pub fn client(timeout: Duration) -> Result<Client, String> {
    TLS.call_once(|| {
        // reqwest is built without a crypto provider of its own: ring, installed once.
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
    reqwest::blocking::Client::builder()
        .https_only(!cfg!(test))
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(20))
        .timeout(timeout)
        .user_agent(concat!("FlightInsights/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|e| format!("could not set up HTTPS: {e}"))
}

/// Whether the app may send a request to `url`.
pub fn allowed(url: &Url) -> Result<(), String> {
    let host = url.host_str().unwrap_or_default();
    #[cfg(test)]
    if url.scheme() == "http" && host == "127.0.0.1" {
        return Ok(()); // the tests' own local fake servers
    }
    if url.scheme() != "https" {
        return Err("only HTTPS requests are allowed".into());
    }
    if !ALLOWED_HOSTS.contains(&host) || url.port().is_some_and(|p| p != 443) {
        return Err(format!("{host} is not an endpoint this app talks to"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_allowed_endpoints_over_https() {
        let ok = |u: &str| allowed(&Url::parse(u).unwrap()).is_ok();
        assert!(ok("https://api.anthropic.com/v1/messages"));
        assert!(!ok("http://api.anthropic.com/v1/messages"));
        assert!(!ok("https://api.anthropic.com:8443/v1/messages"));
        assert!(!ok("https://api.anthropic.com.evil.example/v1/messages"));
        assert!(!ok("https://example.com/"));
        assert!(!ok("https://user@example.com@api.anthropic.com.example/"));
        // the five endpoints, and nothing else: the free Open-Meteo API is not for
        // commercial use and takes no key
        assert!(ok("https://customer-api.open-meteo.com/v1/forecast"));
        assert!(ok("https://customer-archive-api.open-meteo.com/v1/archive"));
        assert!(ok(
            "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
        ));
        assert!(!ok("https://api.open-meteo.com/v1/forecast"));
        assert!(!ok("https://archive-api.open-meteo.com/v1/archive"));
        assert!(!ok("https://open-meteo.com/"));
        assert!(!ok("https://swpc.noaa.gov/"));
        assert_eq!(ALLOWED_HOSTS.len(), 6);
    }

    #[test]
    fn no_other_module_builds_a_client() {
        // reqwest is named only here, so no request can skip the checks above.
        fn walk(dir: &std::path::Path, files: &mut Vec<std::path::PathBuf>) {
            for entry in std::fs::read_dir(dir).unwrap().flatten() {
                let path = entry.path();
                if path.is_dir() {
                    walk(&path, files);
                } else if path.extension().is_some_and(|e| e == "rs") {
                    files.push(path);
                }
            }
        }
        let mut files = Vec::new();
        walk(
            &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
            &mut files,
        );
        assert!(
            files.iter().any(|p| p.ends_with("sync/object.rs")),
            "the walk reaches sub-folders"
        );
        for path in files {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            let text = std::fs::read_to_string(&path).unwrap();
            if name != "net.rs" {
                assert!(!text.contains("reqwest"), "{name} uses reqwest directly");
                assert!(
                    !text.contains("TcpStream::connect"),
                    "{name} opens a socket"
                );
            }
        }
    }
}
