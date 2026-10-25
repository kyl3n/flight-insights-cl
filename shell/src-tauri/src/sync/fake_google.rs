//! A stand-in for Google, for tests only: the token endpoint and the slice of Drive v3
//! the client uses (files, folders, resumable uploads, media downloads, the change
//! feed), held in memory. It can be told to fail in the ways the real one does.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::{Value, json};

use crate::net::Url;
use crate::sync::oauth::Endpoints;
use crate::sync::sodium;

#[derive(Debug, Clone)]
pub struct File {
    pub name: String,
    pub parent: String,
    pub folder: bool,
    pub data: Vec<u8>,
}

#[derive(Default)]
pub struct State {
    pub files: BTreeMap<String, File>,
    pub changes: Vec<String>, // file ids, in order; a page token is an index
    next: u64,
    sessions: BTreeMap<String, (String, String, u64, Vec<u8>)>, // id -> name, parent, total, bytes
    codes: BTreeMap<String, String>,                            // code -> PKCE challenge
    pub refresh_tokens: Vec<String>,
    pub access_tokens: Vec<String>,
    /// Failures to serve next: "429", "500", "drop-chunk", "expire".
    pub fail: Vec<&'static str>,
    pub requests: Vec<String>,
}

pub struct FakeGoogle {
    pub state: Arc<Mutex<State>>,
    pub base: String,
}

pub const DRIVE: &str = "shared-drive-1";

impl FakeGoogle {
    pub fn start() -> FakeGoogle {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let state = Arc::new(Mutex::new(State::default()));
        let shared = state.clone();
        let me = base.clone();
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let state = shared.clone();
                let me = me.clone();
                thread::spawn(move || {
                    let _ = serve(stream, &state, &me);
                });
            }
        });
        FakeGoogle { state, base }
    }

    pub fn endpoints(&self) -> Endpoints {
        Endpoints {
            auth: format!("{}/auth", self.base),
            token: format!("{}/token", self.base),
            api: format!("{}/drive/v3", self.base),
            upload: format!("{}/upload/drive/v3", self.base),
        }
    }

    /// What the consent page does once a person agrees: a code for this challenge.
    pub fn consent(&self, auth_url: &str) -> (String, String, String) {
        let url = Url::parse(auth_url).unwrap();
        let get = |n: &str| {
            url.query_pairs()
                .find(|(k, _)| k == n)
                .map(|(_, v)| v.into_owned())
                .unwrap()
        };
        let code = format!("code-{}", hex::encode(sodium::random::<6>().as_ref()));
        self.state
            .lock()
            .unwrap()
            .codes
            .insert(code.clone(), get("code_challenge"));
        (get("redirect_uri"), code, get("state"))
    }

    /// A refresh token as if the account had signed in before.
    pub fn refresh_token(&self) -> String {
        let t = format!("refresh-{}", hex::encode(sodium::random::<6>().as_ref()));
        self.state.lock().unwrap().refresh_tokens.push(t.clone());
        t
    }

    pub fn fail(&self, what: &'static str) {
        self.state.lock().unwrap().fail.push(what);
    }
}

fn respond(
    stream: &mut TcpStream,
    status: u16,
    headers: &[(&str, String)],
    body: &[u8],
) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        308 => "Resume Incomplete",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        429 => "Too Many Requests",
        _ => "Error",
    };
    write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    )?;
    for (k, v) in headers {
        write!(stream, "{k}: {v}\r\n")?;
    }
    write!(stream, "\r\n")?;
    stream.write_all(body)?;
    stream.flush()
}

fn json_reply(stream: &mut TcpStream, status: u16, v: &Value) -> std::io::Result<()> {
    respond(
        stream,
        status,
        &[("Content-Type", "application/json".into())],
        v.to_string().as_bytes(),
    )
}

fn serve(mut stream: TcpStream, state: &Arc<Mutex<State>>, base: &str) -> std::io::Result<()> {
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut line = String::new();
    reader.read_line(&mut line)?;
    let mut parts = line.split_whitespace();
    let (method, target) = (
        parts.next().unwrap_or("").to_string(),
        parts.next().unwrap_or("/").to_string(),
    );
    let mut headers = BTreeMap::new();
    loop {
        let mut h = String::new();
        reader.read_line(&mut h)?;
        let h = h.trim_end();
        if h.is_empty() {
            break;
        }
        if let Some((k, v)) = h.split_once(':') {
            headers.insert(k.trim().to_lowercase(), v.trim().to_string());
        }
    }
    let len: usize = headers
        .get("content-length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let mut body = vec![0u8; len];
    reader.read_exact(&mut body)?;
    let url = Url::parse(&format!("{base}{target}")).unwrap();
    let q = |n: &str| {
        url.query_pairs()
            .find(|(k, _)| k == n)
            .map(|(_, v)| v.into_owned())
    };
    let path = url.path().to_string();
    let mut s = state.lock().unwrap();
    s.requests.push(format!("{method} {path}"));

    if path == "/token" {
        let form_url =
            Url::parse(&format!("http://form/?{}", String::from_utf8_lossy(&body))).unwrap();
        let form: BTreeMap<String, String> = form_url.query_pairs().into_owned().collect();
        let issue = |s: &mut State| {
            let t = format!("access-{}", hex::encode(sodium::random::<6>().as_ref()));
            s.access_tokens.push(t.clone());
            t
        };
        match form.get("grant_type").map(String::as_str) {
            Some("authorization_code") => {
                let challenge = s.codes.remove(form.get("code").map_or("", |c| c.as_str()));
                let verifier = form.get("code_verifier").cloned().unwrap_or_default();
                let ours = base64_url(&sodium::sha256(verifier.as_bytes()));
                if challenge.as_deref() != Some(ours.as_str()) {
                    return json_reply(&mut stream, 400, &json!({ "error": "invalid_grant" }));
                }
                let refresh = format!("refresh-{}", hex::encode(sodium::random::<6>().as_ref()));
                s.refresh_tokens.push(refresh.clone());
                let access = issue(&mut s);
                return json_reply(
                    &mut stream,
                    200,
                    &json!({ "access_token": access, "expires_in": 3600, "refresh_token": refresh }),
                );
            }
            Some("refresh_token") => {
                if !s
                    .refresh_tokens
                    .contains(form.get("refresh_token").unwrap_or(&String::new()))
                {
                    return json_reply(&mut stream, 400, &json!({ "error": "invalid_grant" }));
                }
                let access = issue(&mut s);
                return json_reply(
                    &mut stream,
                    200,
                    &json!({ "access_token": access, "expires_in": 3600 }),
                );
            }
            _ => {
                return json_reply(
                    &mut stream,
                    400,
                    &json!({ "error": "unsupported_grant_type" }),
                );
            }
        }
    }

    // everything else needs a token
    let token = headers
        .get("authorization")
        .and_then(|a| a.strip_prefix("Bearer "))
        .unwrap_or("")
        .to_string();
    if let Some(i) = s.fail.iter().position(|f| *f == "expire") {
        s.fail.remove(i);
        s.access_tokens.clear();
    }
    if !s.access_tokens.contains(&token) {
        return json_reply(&mut stream, 401, &json!({ "error": { "code": 401 } }));
    }
    for code in ["429", "500"] {
        if let Some(i) = s.fail.iter().position(|f| *f == code) {
            s.fail.remove(i);
            return json_reply(
                &mut stream,
                code.parse().unwrap(),
                &json!({ "error": { "code": code } }),
            );
        }
    }

    if method == "GET" && path == "/drive/v3/files" {
        // as Google answers for a drive the account is not a member of, or a wrong id
        if let Some(d) = q("driveId").filter(|d| d != DRIVE) {
            let message = format!("Shared drive not found: {d}");
            return json_reply(
                &mut stream,
                404,
                &json!({ "error": { "code": 404, "message": message } }),
            );
        }
        // "'PARENT' in parents and trashed = false[ and name = 'NAME']"
        let query = q("q").unwrap_or_default();
        let parent = query.split('\'').nth(1).unwrap_or("").to_string();
        let name = query
            .split(" and name = '")
            .nth(1)
            .map(|n| n.trim_end_matches('\'').replace("\\'", "'"));
        let all: Vec<(&String, &File)> = s
            .files
            .iter()
            .filter(|(_, f)| f.parent == parent && name.as_ref().is_none_or(|n| &f.name == n))
            .collect();
        // pages of two, to exercise paging
        let start: usize = q("pageToken").and_then(|t| t.parse().ok()).unwrap_or(0);
        let page: Vec<Value> = all.iter().skip(start).take(2).map(|(id, f)| {
            json!({ "id": id, "name": f.name, "size": f.data.len().to_string(),
                    "mimeType": if f.folder { "application/vnd.google-apps.folder" } else { "application/octet-stream" } })
        }).collect();
        let mut reply = json!({ "files": page });
        if start + 2 < all.len() {
            reply["nextPageToken"] = json!((start + 2).to_string());
        }
        return json_reply(&mut stream, 200, &reply);
    }
    if method == "POST" && path == "/drive/v3/files" {
        let meta: Value = serde_json::from_slice(&body).unwrap();
        s.next += 1;
        let id = format!("f{:05}", s.next);
        s.files.insert(
            id.clone(),
            File {
                name: meta["name"].as_str().unwrap().into(),
                parent: meta["parents"][0].as_str().unwrap().into(),
                folder: meta["mimeType"] == "application/vnd.google-apps.folder",
                data: vec![],
            },
        );
        s.changes.push(id.clone());
        return json_reply(&mut stream, 200, &json!({ "id": id }));
    }
    if method == "POST"
        && path == "/upload/drive/v3/files"
        && q("uploadType").as_deref() == Some("resumable")
    {
        let meta: Value = serde_json::from_slice(&body).unwrap();
        let total: u64 = headers
            .get("x-upload-content-length")
            .and_then(|v| v.parse().ok())
            .unwrap();
        s.next += 1;
        let session = format!("s{}", s.next);
        s.sessions.insert(
            session.clone(),
            (
                meta["name"].as_str().unwrap().into(),
                meta["parents"][0].as_str().unwrap().into(),
                total,
                vec![],
            ),
        );
        let location = format!("{base}/upload/session/{session}");
        return respond(&mut stream, 200, &[("Location", location)], b"");
    }
    if method == "PUT" && path.starts_with("/upload/session/") {
        let session = path.trim_start_matches("/upload/session/").to_string();
        let range = headers.get("content-range").cloned().unwrap_or_default();
        let drop = !body.is_empty()
            && s.fail
                .iter()
                .position(|f| *f == "drop-chunk")
                .map(|i| s.fail.remove(i))
                .is_some();
        let Some((name, parent, total, mut have)) = s.sessions.remove(&session) else {
            return json_reply(&mut stream, 404, &json!({}));
        };
        if !drop && !body.is_empty() {
            // "bytes a-b/total": a chunk is taken only where the last one ended
            let from: u64 = range
                .trim_start_matches("bytes ")
                .split('-')
                .next()
                .unwrap()
                .parse()
                .unwrap();
            if from == have.len() as u64 {
                have.extend_from_slice(&body);
            }
        }
        if have.len() as u64 == total {
            s.next += 1;
            let id = format!("f{:05}", s.next);
            s.files.insert(
                id.clone(),
                File {
                    name,
                    parent,
                    folder: false,
                    data: have,
                },
            );
            s.changes.push(id.clone());
            return json_reply(&mut stream, 200, &json!({ "id": id }));
        }
        let held = have.len();
        s.sessions.insert(session, (name, parent, total, have));
        if drop {
            drop_connection(stream);
            return Ok(());
        }
        let headers: Vec<(&str, String)> = if held > 0 {
            vec![("Range", format!("bytes=0-{}", held - 1))]
        } else {
            vec![]
        };
        return respond(&mut stream, 308, &headers, b"");
    }
    if method == "GET" && path == "/drive/v3/drives" {
        return json_reply(
            &mut stream,
            200,
            &json!({ "drives": [{ "id": DRIVE, "name": "Fleet data" }] }),
        );
    }
    if method == "GET" && path == "/drive/v3/changes/startPageToken" {
        let token = s.changes.len().to_string();
        return json_reply(&mut stream, 200, &json!({ "startPageToken": token }));
    }
    if method == "GET" && path == "/drive/v3/changes" {
        let from: usize = q("pageToken").and_then(|t| t.parse().ok()).unwrap_or(0);
        let changes: Vec<Value> = s
            .changes
            .iter()
            .skip(from)
            .map(|id| json!({ "fileId": id }))
            .collect();
        return json_reply(
            &mut stream,
            200,
            &json!({ "changes": changes, "newStartPageToken": s.changes.len().to_string() }),
        );
    }
    if method == "GET" && path.starts_with("/drive/v3/files/") {
        let id = path.trim_start_matches("/drive/v3/files/");
        let Some(file) = s.files.get(id) else {
            return json_reply(&mut stream, 404, &json!({}));
        };
        if q("alt").as_deref() == Some("media") {
            let data = file.data.clone();
            drop(s);
            return respond(&mut stream, 200, &[], &data);
        }
        return json_reply(
            &mut stream,
            200,
            &json!({ "id": id, "sha256Checksum": hex::encode(sodium::sha256(&file.data)) }),
        );
    }
    json_reply(
        &mut stream,
        404,
        &json!({ "error": format!("{method} {path}") }),
    )
}

fn drop_connection(stream: TcpStream) {
    let _ = stream.shutdown(std::net::Shutdown::Both);
}

fn base64_url(data: &[u8]) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(data)
}
