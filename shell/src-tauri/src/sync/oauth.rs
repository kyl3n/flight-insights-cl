//! Signing in to Google for the shared drive (spec: Shared storage and sync, "Setup"):
//! an Internal OAuth app, the installed-app flow with a loopback redirect and PKCE.
//!
//! 1. A one-off listener on `127.0.0.1` at a random port is the redirect target.
//! 2. The system browser opens Google's consent page with a PKCE challenge and a
//!    random `state`; the app itself never sees the password.
//! 3. Google redirects back with a code, which is exchanged once for an access token
//!    (kept in memory) and a refresh token (kept in the OS keychain by the caller).
//!
//! The client ID and Google's desktop "secret" (not confidential for installed apps)
//! are built in from CI secrets; a build without them cannot sign in, and says so.
//! Tokens are never logged.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde::Deserialize;
use zeroize::Zeroizing;

use crate::net::{self, Url};
use crate::sync::sodium;

/// Full access to the shared drive: objects other laptops wrote must be readable, which
/// `drive.file` would hide. An Internal app needs no verification for it.
pub const SCOPE: &str = "https://www.googleapis.com/auth/drive";

/// The OAuth client this build was made with, from CI secrets.
pub fn built_in_client() -> Option<Client> {
    Some(Client {
        id: option_env!("FI_GOOGLE_CLIENT_ID")
            .filter(|s| !s.is_empty())?
            .into(),
        secret: option_env!("FI_GOOGLE_CLIENT_SECRET")
            .unwrap_or_default()
            .into(),
    })
}

#[derive(Clone)]
pub struct Client {
    pub id: String,
    pub secret: String,
}

impl std::fmt::Debug for Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Client")
            .field("id", &self.id)
            .finish_non_exhaustive()
    }
}

/// Where Google is. Tests point it at their own fake server.
#[derive(Debug, Clone)]
pub struct Endpoints {
    pub auth: String,
    pub token: String,
    pub api: String,
    pub upload: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Endpoints {
            auth: "https://accounts.google.com/o/oauth2/v2/auth".into(),
            token: "https://oauth2.googleapis.com/token".into(),
            api: "https://www.googleapis.com/drive/v3".into(),
            upload: "https://www.googleapis.com/upload/drive/v3".into(),
        }
    }
}

#[derive(Deserialize)]
struct TokenReply {
    access_token: String,
    expires_in: Option<u64>,
    refresh_token: Option<String>,
}

#[derive(Deserialize)]
struct TokenError {
    error: String,
}

/// An access token and when it stops working.
pub struct Access {
    pub token: Zeroizing<String>,
    pub until: Instant,
}

pub struct SignedIn {
    pub refresh: Zeroizing<String>,
    pub access: Access,
}

#[derive(Debug, PartialEq, Eq)]
pub enum OAuthError {
    /// The refresh token was revoked or expired: sign in again.
    SignInAgain,
    Other(String),
}

impl std::fmt::Display for OAuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OAuthError::SignInAgain => f.write_str("Google needs this laptop to sign in again"),
            OAuthError::Other(e) => f.write_str(e),
        }
    }
}

/// A form body, URL-encoded.
pub fn form(fields: &[(&str, &str)]) -> String {
    let mut url = Url::parse("http://form/").expect("a fixed URL");
    url.query_pairs_mut().extend_pairs(fields);
    url.query().unwrap_or_default().to_string()
}

fn token_request(endpoints: &Endpoints, form: &[(&str, &str)]) -> Result<TokenReply, OAuthError> {
    let url = Url::parse(&endpoints.token).map_err(|e| OAuthError::Other(e.to_string()))?;
    net::allowed(&url).map_err(OAuthError::Other)?;
    let http = net::client(Duration::from_secs(30)).map_err(OAuthError::Other)?;
    let reply = http
        .post(url)
        .header("Content-Type", "application/x-www-form-urlencoded")
        .body(self::form(form))
        .send()
        .map_err(|e| OAuthError::Other(format!("Google could not be reached: {e}")))?;
    let status = reply.status();
    let body = reply.text().map_err(|e| OAuthError::Other(e.to_string()))?;
    if status.is_success() {
        return serde_json::from_str(&body)
            .map_err(|_| OAuthError::Other("Google's token reply is not readable".into()));
    }
    match serde_json::from_str::<TokenError>(&body) {
        Ok(e) if e.error == "invalid_grant" => Err(OAuthError::SignInAgain),
        Ok(e) => Err(OAuthError::Other(format!(
            "Google refused the sign-in: {}",
            e.error
        ))),
        Err(_) => Err(OAuthError::Other(format!(
            "Google refused the sign-in ({status})"
        ))),
    }
}

/// A fresh access token from the refresh token.
pub fn refresh(
    endpoints: &Endpoints,
    client: &Client,
    refresh_token: &str,
) -> Result<Access, OAuthError> {
    let reply = token_request(
        endpoints,
        &[
            ("grant_type", "refresh_token"),
            ("refresh_token", refresh_token),
            ("client_id", &client.id),
            ("client_secret", &client.secret),
        ],
    )?;
    Ok(Access {
        token: Zeroizing::new(reply.access_token),
        until: Instant::now()
            + Duration::from_secs(reply.expires_in.unwrap_or(3600).saturating_sub(60)),
    })
}

fn random_text(bytes: usize) -> String {
    let raw = sodium::random::<48>();
    URL_SAFE_NO_PAD.encode(&raw[..bytes.min(48)])
}

/// Runs the sign-in: `open` shows the consent page in the system browser. Waits up to
/// `wait` for Google to send the browser back.
pub fn sign_in(
    endpoints: &Endpoints,
    client: &Client,
    open: &dyn Fn(&str) -> Result<(), String>,
    wait: Duration,
) -> Result<SignedIn, OAuthError> {
    let other = |e: std::io::Error| OAuthError::Other(format!("the sign-in listener: {e}"));
    let listener = TcpListener::bind("127.0.0.1:0").map_err(other)?;
    let redirect = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().map_err(other)?.port()
    );
    let verifier = Zeroizing::new(random_text(48)); // 64 characters, as RFC 7636 allows
    let challenge = URL_SAFE_NO_PAD.encode(sodium::sha256(verifier.as_bytes()));
    let state = random_text(24);
    let mut url = Url::parse(&endpoints.auth).map_err(|e| OAuthError::Other(e.to_string()))?;
    url.query_pairs_mut()
        .append_pair("client_id", &client.id)
        .append_pair("redirect_uri", &redirect)
        .append_pair("response_type", "code")
        .append_pair("scope", SCOPE)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256")
        .append_pair("state", &state)
        .append_pair("access_type", "offline")
        .append_pair("prompt", "consent");
    open(url.as_str()).map_err(OAuthError::Other)?;
    let code = wait_for_code(&listener, &state, wait)?;
    let reply = token_request(
        endpoints,
        &[
            ("grant_type", "authorization_code"),
            ("code", &code),
            ("redirect_uri", &redirect),
            ("client_id", &client.id),
            ("client_secret", &client.secret),
            ("code_verifier", &verifier),
        ],
    )?;
    let refresh = reply
        .refresh_token
        .ok_or_else(|| OAuthError::Other("Google gave no refresh token".into()))?;
    Ok(SignedIn {
        refresh: Zeroizing::new(refresh),
        access: Access {
            token: Zeroizing::new(reply.access_token),
            until: Instant::now()
                + Duration::from_secs(reply.expires_in.unwrap_or(3600).saturating_sub(60)),
        },
    })
}

/// The browser's one request to the loopback address: the code, if `state` matches.
fn wait_for_code(
    listener: &TcpListener,
    state: &str,
    wait: Duration,
) -> Result<Zeroizing<String>, OAuthError> {
    let deadline = Instant::now() + wait;
    listener
        .set_nonblocking(true)
        .map_err(|e| OAuthError::Other(e.to_string()))?;
    loop {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream.set_nonblocking(false).ok();
                stream.set_read_timeout(Some(Duration::from_secs(10))).ok();
                let mut line = String::new();
                BufReader::new(&stream).read_line(&mut line).ok();
                // GET /?state=...&code=... HTTP/1.1
                let target = line.split_whitespace().nth(1).unwrap_or("/");
                let query = Url::parse(&format!("http://127.0.0.1{target}")).ok();
                let get = |name: &str| {
                    query.as_ref().and_then(|u| {
                        u.query_pairs()
                            .find(|(k, _)| k == name)
                            .map(|(_, v)| v.into_owned())
                    })
                };
                let (ok, page) = if get("state").as_deref() != Some(state) {
                    (
                        None,
                        "This sign-in link is not the one the app opened. Close this window and try again.",
                    )
                } else if let Some(code) = get("code") {
                    (
                        Some(code),
                        "Signed in. You can close this window and return to Flight Insights.",
                    )
                } else {
                    (
                        None,
                        "The sign-in was not completed. Close this window and try again from the app.",
                    )
                };
                let body = format!(
                    "<!doctype html><meta charset=utf-8><title>Flight Insights</title><p>{page}"
                );
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                if get("state").as_deref() != Some(state) {
                    continue; // not ours: keep waiting for the real one
                }
                return ok.map(Zeroizing::new).ok_or_else(|| {
                    OAuthError::Other(format!(
                        "the sign-in was not completed ({})",
                        get("error").unwrap_or_else(|| "no code".into())
                    ))
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() > deadline {
                    return Err(OAuthError::Other("the sign-in timed out".into()));
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => return Err(OAuthError::Other(e.to_string())),
        }
    }
}
