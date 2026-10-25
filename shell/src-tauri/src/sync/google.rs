//! The shared drive on Google Drive (spec: Shared storage and sync): the `Drive` the app
//! uses outside tests, scoped to one shared drive and one folder in it.
//!
//! Paths map to folders under "Flight Insights" (`devices/<laptop>/`, `registry/`),
//! created the first time they are needed. Uploads are resumable, in 8 MiB chunks, and
//! carry on after a dropped connection; the SHA-256 Drive reports is returned for the
//! loop to compare with its own. A name that exists is never written again, and nothing
//! is ever deleted. Every request goes through `net.rs`; a refused access token is
//! refreshed once, and Google's "slow down" and server errors are retried with backoff.
//!
//! Two laptops may create the same folder at the same moment, and Drive keeps both:
//! a path then stands for all folders of that name, and new files go into the one with
//! the smallest id, so every laptop picks the same.

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use zeroize::Zeroizing;

use crate::net::{self, RequestBuilder, Response, StatusCode, Url};
use crate::sync::drive::{Drive, Entry};
use crate::sync::oauth::{self, Access, Client, Endpoints, OAuthError};

pub const ROOT: &str = "Flight Insights";
const FOLDER: &str = "application/vnd.google-apps.folder";
const CHUNK: usize = 8 << 20; // a multiple of 256 KiB, as resumable uploads require
const TRIES: u32 = 5;
const FOLDER_TTL: Duration = Duration::from_secs(300);

pub struct GoogleDrive {
    endpoints: Endpoints,
    client: Client,
    drive_id: String,
    refresh: Zeroizing<String>,
    access: Mutex<Option<Access>>,
    folders: Mutex<HashMap<String, (Vec<String>, Instant)>>,
    http: net::Client,
    backoff: Duration,
    /// The one folder in the shared drive everything lives under ("Flight Insights").
    root: String,
}

fn other(e: impl std::fmt::Display) -> io::Error {
    io::Error::other(e.to_string())
}

fn quoted(name: &str) -> String {
    name.replace('\\', "\\\\").replace('\'', "\\'")
}

impl GoogleDrive {
    pub fn new(
        endpoints: Endpoints,
        client: Client,
        drive_id: &str,
        refresh: Zeroizing<String>,
    ) -> io::Result<GoogleDrive> {
        Ok(GoogleDrive {
            endpoints,
            client,
            drive_id: drive_id.into(),
            refresh,
            access: Mutex::new(None),
            folders: Mutex::new(HashMap::new()),
            http: net::client(Duration::from_secs(300)).map_err(other)?,
            backoff: Duration::from_secs(1),
            root: ROOT.into(),
        })
    }

    /// Everything under another top-level folder of the shared drive: the nightly soak
    /// gives each run its own. One folder name, no slashes.
    pub fn under(mut self, root: &str) -> io::Result<Self> {
        if root.is_empty() || root.contains('/') {
            return Err(other("the root is one folder name"));
        }
        self.root = root.into();
        Ok(self)
    }

    #[cfg(test)]
    fn quick(mut self) -> Self {
        self.backoff = Duration::from_millis(5);
        self
    }

    fn token(&self, renew: bool) -> io::Result<Zeroizing<String>> {
        let mut access = self.access.lock().expect("token");
        let fresh = access.as_ref().is_some_and(|a| a.until > Instant::now());
        if renew || !fresh {
            *access = Some(
                oauth::refresh(&self.endpoints, &self.client, &self.refresh).map_err(
                    |e| match e {
                        OAuthError::SignInAgain => {
                            io::Error::new(io::ErrorKind::PermissionDenied, e.to_string())
                        }
                        OAuthError::Other(m) => io::Error::other(m),
                    },
                )?,
            );
        }
        Ok(access.as_ref().expect("just set").token.clone())
    }

    /// One request, made by `make` for a URL that passed the allowlist: a refused token
    /// is renewed once, and 429 and server errors are retried with growing waits.
    fn send(
        &self,
        url: &str,
        make: &dyn Fn(&net::Client, Url) -> RequestBuilder,
    ) -> io::Result<Response> {
        let url = Url::parse(url).map_err(other)?;
        net::allowed(&url).map_err(other)?;
        let mut renewed = false;
        let mut wait = self.backoff;
        for attempt in 1..=TRIES {
            let token = self.token(false)?;
            let reply = make(&self.http, url.clone())
                .bearer_auth(token.as_str())
                .send();
            match reply {
                Ok(r) if r.status() == StatusCode::UNAUTHORIZED && !renewed => {
                    renewed = true;
                    self.token(true)?;
                }
                Ok(r)
                    if (r.status() == StatusCode::TOO_MANY_REQUESTS
                        || r.status().is_server_error())
                        && attempt < TRIES =>
                {
                    thread::sleep(wait);
                    wait *= 2;
                }
                Ok(r) => return Ok(r),
                Err(e) if attempt < TRIES && (e.is_connect() || e.is_timeout()) => {
                    thread::sleep(wait);
                    wait *= 2;
                }
                Err(e) => return Err(other(format!("Google Drive could not be reached: {e}"))),
            }
        }
        Err(other("Google Drive kept refusing the request"))
    }

    fn json(&self, reply: Response) -> io::Result<Value> {
        let status = reply.status();
        let body = reply.text().map_err(other)?;
        if !status.is_success() {
            // Google's own reason, such as "Shared drive not found", says what to fix
            let why = serde_json::from_str::<Value>(&body).ok().and_then(|v| {
                v["error"]["message"]
                    .as_str()
                    .map(|m| m.chars().take(200).collect::<String>())
            });
            return Err(other(match why {
                Some(m) => format!("Google Drive answered {status}: {m}"),
                None => format!("Google Drive answered {status}"),
            }));
        }
        serde_json::from_str(&body).map_err(|_| other("Google Drive's reply is not readable"))
    }

    /// The children of one folder, every page.
    fn children(&self, parent: &str, name: Option<&str>) -> io::Result<Vec<Value>> {
        let mut q = format!("'{}' in parents and trashed = false", quoted(parent));
        if let Some(n) = name {
            q.push_str(&format!(" and name = '{}'", quoted(n)));
        }
        let mut out = Vec::new();
        let mut page: Option<String> = None;
        loop {
            let mut url = Url::parse(&format!("{}/files", self.endpoints.api)).map_err(other)?;
            {
                let mut p = url.query_pairs_mut();
                p.append_pair("q", &q)
                    .append_pair("driveId", &self.drive_id)
                    .append_pair("corpora", "drive")
                    .append_pair("includeItemsFromAllDrives", "true")
                    .append_pair("supportsAllDrives", "true")
                    .append_pair("pageSize", "1000")
                    .append_pair("fields", "nextPageToken,files(id,name,mimeType,size)");
                if let Some(t) = &page {
                    p.append_pair("pageToken", t);
                }
            }
            let reply = self.json(self.send(url.as_str(), &|h, u| h.get(u))?)?;
            out.extend(reply["files"].as_array().cloned().unwrap_or_default());
            match reply["nextPageToken"].as_str() {
                Some(t) => page = Some(t.into()),
                None => return Ok(out),
            }
        }
    }

    /// The folders a path stands for, smallest id first; created when `create`. Ids are
    /// held a few minutes for writing; a listing looks again (`fresh`), so a twin folder
    /// another laptop made meanwhile is never missed.
    fn folder(&self, path: &str, create: bool, fresh: bool) -> io::Result<Vec<String>> {
        if !fresh
            && let Some((ids, at)) = self.folders.lock().expect("folders").get(path)
            && at.elapsed() < FOLDER_TTL
        {
            return Ok(ids.clone());
        }
        let (parent_path, name) = match path.rsplit_once('/') {
            Some((p, n)) => (p, n),
            None if path.is_empty() => return Ok(vec![self.drive_id.clone()]),
            None => ("", path),
        };
        let parents = if path == self.root {
            vec![self.drive_id.clone()]
        } else {
            self.folder(parent_path, create, fresh)?
        };
        let mut ids: Vec<String> = Vec::new();
        for p in &parents {
            for f in self.children(p, Some(name))? {
                if f["mimeType"] == FOLDER {
                    ids.push(f["id"].as_str().unwrap_or_default().into());
                }
            }
        }
        if ids.is_empty() {
            if !create || parents.is_empty() {
                return Ok(vec![]);
            }
            let url = format!("{}/files?supportsAllDrives=true", self.endpoints.api);
            let meta = json!({ "name": name, "mimeType": FOLDER, "parents": [parents[0]] });
            let made = self.json(self.send(&url, &|h, u| {
                h.post(u)
                    .header("Content-Type", "application/json")
                    .body(meta.to_string())
            })?)?;
            ids.push(
                made["id"]
                    .as_str()
                    .ok_or_else(|| other("no folder id"))?
                    .into(),
            );
        }
        ids.sort();
        self.folders
            .lock()
            .expect("folders")
            .insert(path.into(), (ids.clone(), Instant::now()));
        Ok(ids)
    }

    fn dir_of<'p>(&self, path: &'p str) -> (String, &'p str) {
        match path.rsplit_once('/') {
            Some((d, n)) => (format!("{}/{d}", self.root), n),
            None => (self.root.clone(), path),
        }
    }

    /// A file's id; a miss looks again past the cached folder ids, in case the file
    /// sits in a twin folder another laptop made since they were read.
    fn file_id(&self, path: &str) -> io::Result<Option<String>> {
        let (dir, name) = self.dir_of(path);
        for fresh in [false, true] {
            for folder in self.folder(&dir, false, fresh)? {
                if let Some(f) = self
                    .children(&folder, Some(name))?
                    .into_iter()
                    .find(|f| f["mimeType"] != FOLDER)
                {
                    return Ok(f["id"].as_str().map(String::from));
                }
            }
        }
        Ok(None)
    }

    fn walk(&self, folder: &str, prefix: &str, out: &mut Vec<Entry>) -> io::Result<()> {
        for f in self.children(folder, None)? {
            let name = f["name"].as_str().unwrap_or_default();
            let path = if prefix.is_empty() {
                name.to_string()
            } else {
                format!("{prefix}/{name}")
            };
            if f["mimeType"] == FOLDER {
                self.walk(f["id"].as_str().unwrap_or_default(), &path, out)?;
            } else {
                let size = f["size"].as_str().and_then(|s| s.parse().ok()).unwrap_or(0);
                out.push(Entry { path, size });
            }
        }
        Ok(())
    }

    fn checksum(&self, id: &str) -> io::Result<[u8; 32]> {
        let url = format!(
            "{}/files/{id}?fields=sha256Checksum&supportsAllDrives=true",
            self.endpoints.api
        );
        let reply = self.json(self.send(&url, &|h, u| h.get(u))?)?;
        let mut sum = [0u8; 32];
        hex::decode_to_slice(
            reply["sha256Checksum"].as_str().unwrap_or_default(),
            &mut sum,
        )
        .map_err(|_| other("Google Drive reported no checksum"))?;
        Ok(sum)
    }

    /// A resumable upload: a session, then chunks; after a dropped connection the
    /// session says how much it holds, and the upload carries on from there.
    fn resumable(&self, name: &str, parent: &str, from: &Path) -> io::Result<String> {
        let mut file = File::open(from)?;
        let total = file.metadata()?.len();
        let url = format!(
            "{}/files?uploadType=resumable&supportsAllDrives=true",
            self.endpoints.upload
        );
        let meta = json!({ "name": name, "parents": [parent] });
        let started = self.send(&url, &|h, u| {
            h.post(u)
                .header("X-Upload-Content-Length", total)
                .header("Content-Type", "application/json")
                .body(meta.to_string())
        })?;
        if !started.status().is_success() {
            return Err(other(format!(
                "Google Drive refused the upload ({})",
                started.status()
            )));
        }
        let session = started
            .headers()
            .get("location")
            .and_then(|v| v.to_str().ok())
            .ok_or_else(|| other("Google Drive gave no upload session"))?
            .to_string();
        let mut sent: u64 = 0;
        let mut buf = vec![0u8; CHUNK];
        let mut stalls = 0;
        loop {
            let n = if total == 0 {
                0
            } else {
                file.seek(SeekFrom::Start(sent))?;
                let want = (total - sent).min(CHUNK as u64) as usize;
                file.read_exact(&mut buf[..want])?;
                want
            };
            let range = if total == 0 {
                "bytes */0".to_string()
            } else {
                format!("bytes {sent}-{}/{total}", sent + n as u64 - 1)
            };
            let chunk = buf[..n].to_vec();
            let reply = self.send(&session, &|h, u| {
                h.put(u)
                    .header("Content-Range", range.clone())
                    .body(chunk.clone())
            });
            let reply = match reply {
                Ok(r) => r,
                Err(_) => {
                    // the connection dropped: ask the session what it holds
                    stalls += 1;
                    if stalls > TRIES {
                        return Err(other("the upload kept failing"));
                    }
                    let status = format!("bytes */{total}");
                    self.send(&session, &|h, u| {
                        h.put(u).header("Content-Range", status.clone())
                    })?
                }
            };
            match reply.status().as_u16() {
                200 | 201 => {
                    let done = self.json(reply)?;
                    return done["id"]
                        .as_str()
                        .map(String::from)
                        .ok_or_else(|| other("no file id"));
                }
                308 => {
                    sent = reply
                        .headers()
                        .get("range")
                        .and_then(|v| v.to_str().ok())
                        .and_then(|r| r.rsplit('-').next())
                        .and_then(|end| end.parse::<u64>().ok())
                        .map_or(0, |end| end + 1);
                }
                s => return Err(other(format!("Google Drive refused a chunk ({s})"))),
            }
        }
    }
}

/// The shared drives this account can see, as (id, name), to choose the fleet's.
pub fn shared_drives(
    endpoints: &Endpoints,
    client: &Client,
    refresh: &str,
) -> io::Result<Vec<(String, String)>> {
    let drive = GoogleDrive::new(
        endpoints.clone(),
        client.clone(),
        "",
        Zeroizing::new(refresh.to_string()),
    )?;
    let mut out = Vec::new();
    let mut page: Option<String> = None;
    loop {
        let mut url = Url::parse(&format!("{}/drives", endpoints.api)).map_err(other)?;
        url.query_pairs_mut().append_pair("pageSize", "100");
        if let Some(t) = &page {
            url.query_pairs_mut().append_pair("pageToken", t);
        }
        let reply = drive.json(drive.send(url.as_str(), &|h, u| h.get(u))?)?;
        for d in reply["drives"].as_array().cloned().unwrap_or_default() {
            out.push((
                d["id"].as_str().unwrap_or_default().into(),
                d["name"].as_str().unwrap_or_default().into(),
            ));
        }
        match reply["nextPageToken"].as_str() {
            Some(t) => page = Some(t.into()),
            None => return Ok(out),
        }
    }
}

impl Drive for GoogleDrive {
    fn list(&self, prefix: &str) -> io::Result<Vec<Entry>> {
        // start from the deepest folder the prefix names
        let dir = prefix.rsplit_once('/').map_or("", |(d, _)| d);
        let start = if dir.is_empty() {
            self.root.clone()
        } else {
            format!("{}/{dir}", self.root)
        };
        let mut out = Vec::new();
        for folder in self.folder(&start, false, true)? {
            self.walk(&folder, dir, &mut out)?;
        }
        out.retain(|e| e.path.starts_with(prefix));
        out.sort_by(|a, b| a.path.cmp(&b.path));
        out.dedup_by(|a, b| a.path == b.path);
        Ok(out)
    }

    fn upload(&self, path: &str, from: &Path) -> io::Result<[u8; 32]> {
        let (dir, name) = self.dir_of(path);
        if name.is_empty() || name.starts_with('.') {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "not a drive path",
            ));
        }
        if self.file_id(path)?.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "an object is never replaced",
            ));
        }
        let folder = self
            .folder(&dir, true, false)?
            .into_iter()
            .next()
            .ok_or_else(|| other("no folder"))?;
        let id = self.resumable(name, &folder, from)?;
        self.checksum(&id)
    }

    fn download(&self, path: &str, to: &Path) -> io::Result<()> {
        let id = self
            .file_id(path)?
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no such object"))?;
        let url = format!(
            "{}/files/{id}?alt=media&supportsAllDrives=true",
            self.endpoints.api
        );
        let mut reply = self.send(&url, &|h, u| h.get(u))?;
        if !reply.status().is_success() {
            return Err(other(format!(
                "Google Drive refused the download ({})",
                reply.status()
            )));
        }
        let mut out = File::create(to)?;
        reply.copy_to(&mut out).map_err(other)?;
        out.flush()
    }

    fn changes(&self, cursor: Option<&str>) -> io::Result<(bool, Option<String>)> {
        let Some(cursor) = cursor else {
            let url = format!(
                "{}/changes/startPageToken?driveId={}&supportsAllDrives=true",
                self.endpoints.api, self.drive_id
            );
            let reply = self.json(self.send(&url, &|h, u| h.get(u))?)?;
            return Ok((true, reply["startPageToken"].as_str().map(String::from)));
        };
        let mut any = false;
        let mut page = cursor.to_string();
        loop {
            let mut url = Url::parse(&format!("{}/changes", self.endpoints.api)).map_err(other)?;
            url.query_pairs_mut()
                .append_pair("pageToken", &page)
                .append_pair("driveId", &self.drive_id)
                .append_pair("includeItemsFromAllDrives", "true")
                .append_pair("supportsAllDrives", "true")
                .append_pair("fields", "nextPageToken,newStartPageToken,changes(fileId)");
            let reply = self.json(self.send(url.as_str(), &|h, u| h.get(u))?)?;
            any |= reply["changes"].as_array().is_some_and(|c| !c.is_empty());
            if let Some(next) = reply["nextPageToken"].as_str() {
                page = next.into();
                continue;
            }
            return Ok((
                any,
                reply["newStartPageToken"]
                    .as_str()
                    .map(String::from)
                    .or(Some(page)),
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sync::fake_google::{DRIVE, FakeGoogle};
    use crate::sync::sodium;

    fn temp(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "fi-google-{name}-{}",
            hex::encode(sodium::random::<6>().as_ref())
        ))
    }

    pub(crate) fn drive(fake: &FakeGoogle) -> GoogleDrive {
        let client = Client {
            id: "test-client".into(),
            secret: "not-secret".into(),
        };
        GoogleDrive::new(
            fake.endpoints(),
            client,
            DRIVE,
            Zeroizing::new(fake.refresh_token()),
        )
        .unwrap()
        .quick()
    }

    #[test]
    fn objects_go_up_in_chunks_come_back_whole_and_are_never_replaced() {
        let fake = FakeGoogle::start();
        let d = drive(&fake);
        let src = temp("src");
        let data: Vec<u8> = (0..(CHUNK * 2 + 1234)).map(|i| (i % 251) as u8).collect();
        std::fs::write(&src, &data).unwrap();
        fake.fail("drop-chunk"); // the first chunk's connection drops: the upload resumes
        let sum = d.upload("devices/ab/000000000001-cd.fio", &src).unwrap();
        assert_eq!(sum, sodium::sha256(&data));
        let again = d
            .upload("devices/ab/000000000001-cd.fio", &src)
            .unwrap_err();
        assert_eq!(again.kind(), io::ErrorKind::AlreadyExists);
        let back = temp("back");
        d.download("devices/ab/000000000001-cd.fio", &back).unwrap();
        assert_eq!(std::fs::read(&back).unwrap(), data);
        let empty = temp("empty");
        std::fs::write(&empty, b"").unwrap();
        d.upload("registry/00000000-aa.json", &empty).unwrap();
        assert_eq!(
            d.list("")
                .unwrap()
                .iter()
                .map(|e| e.path.as_str())
                .collect::<Vec<_>>(),
            vec![
                "devices/ab/000000000001-cd.fio",
                "registry/00000000-aa.json"
            ]
        );
        assert_eq!(d.list("devices/").unwrap().len(), 1);
        for p in [&src, &back, &empty] {
            let _ = std::fs::remove_file(p);
        }
    }

    #[test]
    fn a_refused_token_is_renewed_and_busy_answers_are_retried() {
        let fake = FakeGoogle::start();
        let d = drive(&fake);
        let src = temp("x");
        std::fs::write(&src, b"x").unwrap();
        d.upload("registry/a.json", &src).unwrap();
        fake.fail("expire");
        fake.fail("429");
        fake.fail("500");
        assert_eq!(d.list("registry/").unwrap().len(), 1);
        // a revoked refresh token: sign in again
        fake.state.lock().unwrap().refresh_tokens.clear();
        fake.fail("expire");
        let e = d.list("registry/").unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::PermissionDenied);
        let _ = std::fs::remove_file(&src);
    }

    #[test]
    fn many_files_are_listed_across_pages_and_twin_folders() {
        let fake = FakeGoogle::start();
        let a = drive(&fake);
        let b = drive(&fake); // a second laptop
        let src = temp("p");
        std::fs::write(&src, b"p").unwrap();
        a.upload("devices/aa/000000000001-01.fio", &src).unwrap();
        // b creates its own "devices" folder at the same moment, as a race would
        {
            let mut s = fake.state.lock().unwrap();
            let root = s
                .files
                .iter()
                .find(|(_, f)| f.name == ROOT)
                .map(|(id, _)| id.clone())
                .unwrap();
            s.files.insert(
                "f99998".into(),
                crate::sync::fake_google::File {
                    name: "devices".into(),
                    parent: root,
                    folder: true,
                    data: vec![],
                },
            );
            s.files.insert(
                "f99999".into(),
                crate::sync::fake_google::File {
                    name: "bb".into(),
                    parent: "f99998".into(),
                    folder: true,
                    data: vec![],
                },
            );
        }
        for n in 2..=5 {
            b.upload(&format!("devices/bb/{n:012}-02.fio"), &src)
                .unwrap();
        }
        let listed = a.list("devices/").unwrap();
        assert_eq!(listed.len(), 5, "{listed:?}");
        let got = temp("got");
        a.download("devices/bb/000000000002-02.fio", &got).unwrap();
        assert_eq!(std::fs::read(&got).unwrap(), b"p");
        let _ = std::fs::remove_file(&got);
        let _ = std::fs::remove_file(&src);
    }

    #[test]
    fn the_shared_drives_are_listed_to_choose_from() {
        let fake = FakeGoogle::start();
        let client = Client {
            id: "c".into(),
            secret: "s".into(),
        };
        let drives = shared_drives(&fake.endpoints(), &client, &fake.refresh_token()).unwrap();
        assert_eq!(drives, vec![(DRIVE.to_string(), "Fleet data".to_string())]);
    }

    #[test]
    fn a_drive_the_account_cannot_see_is_named_in_the_error() {
        let fake = FakeGoogle::start();
        let client = Client {
            id: "c".into(),
            secret: "s".into(),
        };
        let d = GoogleDrive::new(
            fake.endpoints(),
            client,
            "another-drive",
            Zeroizing::new(fake.refresh_token()),
        )
        .unwrap()
        .quick();
        let e = d.list("registry/").unwrap_err().to_string();
        assert_eq!(
            e,
            "Google Drive answered 404 Not Found: Shared drive not found: another-drive"
        );
    }

    #[test]
    fn the_change_feed_says_when_something_arrived() {
        let fake = FakeGoogle::start();
        let d = drive(&fake);
        let (any, cursor) = d.changes(None).unwrap();
        assert!(any, "a first look lists everything");
        let (any, cursor) = d.changes(cursor.as_deref()).unwrap();
        assert!(!any);
        let src = temp("c");
        std::fs::write(&src, b"c").unwrap();
        d.upload("registry/c.json", &src).unwrap();
        let (any, cursor) = d.changes(cursor.as_deref()).unwrap();
        assert!(any);
        assert!(!d.changes(cursor.as_deref()).unwrap().0);
        let _ = std::fs::remove_file(&src);
    }

    #[test]
    fn sign_in_checks_its_challenge_and_its_state() {
        let fake = FakeGoogle::start();
        let client = Client {
            id: "test-client".into(),
            secret: "s".into(),
        };
        let opened = Mutex::new(None::<String>);
        let signed = thread::scope(|s| {
            let handle = s.spawn(|| {
                let open = |u: &str| {
                    *opened.lock().unwrap() = Some(u.into());
                    Ok(())
                };
                oauth::sign_in(&fake.endpoints(), &client, &open, Duration::from_secs(10))
            });
            // the browser: the person agrees, and Google sends it back to the app
            let url = loop {
                if let Some(u) = opened.lock().unwrap().clone() {
                    break u;
                }
                thread::sleep(Duration::from_millis(5));
            };
            let (redirect, code, state) = fake.consent(&url);
            let http = net::client(Duration::from_secs(5)).unwrap();
            // a stray request with another state is ignored; then Google's own
            let _ = http
                .get(format!("{redirect}/?state=forged&code=evil"))
                .send();
            http.get(format!("{redirect}/?state={state}&code={code}"))
                .send()
                .unwrap();
            handle.join().unwrap()
        })
        .unwrap();
        assert!(
            fake.state
                .lock()
                .unwrap()
                .refresh_tokens
                .contains(&*signed.refresh)
        );
        assert!(signed.access.until > Instant::now());
        let url = opened.lock().unwrap().clone().unwrap();
        assert!(url.contains("code_challenge_method=S256") && url.contains("access_type=offline"));
        assert!(url.contains("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive"));
    }

    #[test]
    fn a_wrong_verifier_gets_no_tokens() {
        let fake = FakeGoogle::start();
        let (_, code, _) = fake.consent("http://x/?redirect_uri=r&state=s&code_challenge=not-ours");
        let http = net::client(Duration::from_secs(5)).unwrap();
        let reply = http
            .post(format!("{}/token", fake.base))
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body(oauth::form(&[
                ("grant_type", "authorization_code"),
                ("code", &code),
                ("code_verifier", "guess"),
            ]))
            .send()
            .unwrap();
        assert_eq!(reply.status().as_u16(), 400);
    }
}
