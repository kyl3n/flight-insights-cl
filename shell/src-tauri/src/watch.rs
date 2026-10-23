//! Watched save folders (docs/SPEC.md, "Ground station profiles", setup step 5).
//!
//! Every poll the shell walks each station's save folders for onboard logs, tlogs
//! and parameter files. A file is ready once its size and modification time have
//! held still for a whole poll, since ground station software writes tlogs while
//! it runs. The engine then says which ready files are new (it hashes them;
//! files already imported or set aside are not). New files are only offered:
//! nothing is imported until a person confirms the batch in the import dialog.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::{Deserialize, Serialize};

pub const POLL: Duration = Duration::from_secs(15);
const SUFFIXES: &[&str] = &["bin", "tlog", "param", "params"];
const MAX_DEPTH: usize = 6; // Mission Planner keeps logs under logs/<vehicle>/<sysid>/
// Per folder and poll: a wrong folder (a home folder, a whole drive) must not stall the app.
const MAX_FILES: usize = 20_000;
const MAX_ENTRIES: usize = 200_000; // directory entries visited, files or not

/// A watched folder, as `station.folders` gives it.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct Folder {
    pub id: String,
    pub station_id: String,
    pub station: String,
    pub station_version_id: Option<String>,
    pub path: String,
}

/// A new file waiting for a person to import it.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Pending {
    pub path: String,
    pub name: String,
    pub size: u64,
    pub station_id: String,
    pub station: String,
    pub station_version_id: Option<String>,
}

#[derive(Clone, Debug)]
struct Seen {
    size: u64,
    modified: Option<SystemTime>,
    steady: bool, // unchanged since the last poll
    checked: bool,
    folder: Folder,
}

impl Seen {
    fn new(size: u64, modified: Option<SystemTime>, folder: &Folder) -> Self {
        Seen {
            size,
            modified,
            steady: false,
            checked: false,
            folder: folder.clone(),
        }
    }
}

#[derive(Default)]
pub struct Watcher {
    seen: HashMap<PathBuf, Seen>,
    pending: Vec<Pending>,
}

fn wanted(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| SUFFIXES.contains(&e.to_ascii_lowercase().as_str()))
}

type Found = Vec<(PathBuf, u64, Option<SystemTime>)>;

/// Log and parameter files under `root`, not following links.
pub fn walk(root: &Path) -> Found {
    walk_limited(root, MAX_ENTRIES, MAX_FILES)
}

fn walk_limited(root: &Path, max_entries: usize, max_files: usize) -> Found {
    let mut out = Vec::new();
    let mut visited = 0usize;
    let mut stack = vec![(root.to_path_buf(), 0usize)];
    while let Some((dir, depth)) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            visited += 1;
            if visited > max_entries {
                return out;
            }
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            let path = entry.path();
            if kind.is_dir() && depth < MAX_DEPTH {
                stack.push((path, depth + 1));
            } else if kind.is_file() && wanted(&path) {
                if let Ok(meta) = entry.metadata() {
                    out.push((path, meta.len(), meta.modified().ok()));
                }
                if out.len() >= max_files {
                    return out;
                }
            }
        }
    }
    out
}

/// Walk the folders. Done without the watcher, whose lock the import dialog
/// waits on, since a large folder takes a while; `Watcher::update` takes the result.
pub fn scan(folders: &[Folder]) -> Vec<(Folder, Found)> {
    folders
        .iter()
        .map(|f| (f.clone(), walk(Path::new(&f.path))))
        .collect()
}

impl Watcher {
    /// Take a `scan` of the folders. Returns the files that are ready and not yet checked.
    pub fn update(&mut self, scanned: Vec<(Folder, Found)>) -> Vec<PathBuf> {
        let mut present: HashMap<PathBuf, ()> = HashMap::new();
        for (folder, found) in scanned {
            for (path, size, modified) in found {
                present.insert(path.clone(), ());
                match self.seen.get_mut(&path) {
                    Some(s) if s.size == size && s.modified == modified => {
                        s.steady = true;
                        s.folder = folder.clone();
                    }
                    Some(s) => {
                        *s = Seen::new(size, modified, &folder);
                    }
                    None => {
                        self.seen.insert(path, Seen::new(size, modified, &folder));
                    }
                }
            }
        }
        // Files that went away, or changed again, are no longer offered.
        self.seen.retain(|p, _| present.contains_key(p));
        let seen = &self.seen;
        self.pending.retain(|p| {
            seen.get(Path::new(&p.path))
                .is_some_and(|s| s.steady && s.checked)
        });
        let mut ready: Vec<PathBuf> = self
            .seen
            .iter()
            .filter(|(_, s)| s.steady && !s.checked)
            .map(|(p, _)| p.clone())
            .collect();
        ready.sort();
        ready
    }

    /// The engine's answer for checked files: those in `new` are offered.
    pub fn checked(&mut self, paths: &[PathBuf], new: &[String]) {
        for path in paths {
            let Some(s) = self.seen.get_mut(path) else {
                continue;
            };
            s.checked = true;
            let text = path.to_string_lossy().into_owned();
            let offered = self.pending.iter().any(|p| p.path == text);
            if new.contains(&text) && !offered {
                let folder = &s.folder;
                self.pending.push(Pending {
                    name: path
                        .file_name()
                        .map(|n| n.to_string_lossy().into_owned())
                        .unwrap_or_default(),
                    path: text,
                    size: s.size,
                    station_id: folder.station_id.clone(),
                    station: folder.station.clone(),
                    station_version_id: folder.station_version_id.clone(),
                });
            } else if !new.contains(&text) {
                self.pending.retain(|p| p.path != text);
            }
        }
        self.pending.sort_by(|a, b| a.path.cmp(&b.path));
    }

    /// Ask again about every offered file, after an import or a dismissal.
    pub fn recheck(&mut self) -> Vec<PathBuf> {
        self.pending
            .iter()
            .map(|p| PathBuf::from(&p.path))
            .collect()
    }

    pub fn pending(&self) -> &[Pending] {
        &self.pending
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder(path: &Path) -> Folder {
        Folder {
            id: "f".into(),
            station_id: "s".into(),
            station: "Field tablet".into(),
            station_version_id: Some("v".into()),
            path: path.to_string_lossy().into_owned(),
        }
    }

    /// One poll, as the folder watcher does it: scan, then update.
    fn poll(w: &mut Watcher, folders: &[Folder]) -> Vec<PathBuf> {
        w.update(scan(folders))
    }

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("fi-watch-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn only_log_and_parameter_files_are_found_at_a_sane_depth() {
        let dir = temp("walk");
        let deep = dir.join("logs/QUADROTOR/1");
        std::fs::create_dir_all(&deep).unwrap();
        std::fs::write(deep.join("2026-10-18 14-06-12.tlog"), b"x").unwrap();
        std::fs::write(dir.join("00000142.BIN"), b"x").unwrap();
        std::fs::write(dir.join("notes.txt"), b"x").unwrap();
        std::fs::write(dir.join("quad.params"), b"x").unwrap();
        let mut names: Vec<String> = walk(&dir)
            .into_iter()
            .map(|(p, _, _)| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        names.sort();
        assert_eq!(
            names,
            ["00000142.BIN", "2026-10-18 14-06-12.tlog", "quad.params"]
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_huge_folder_is_walked_only_so_far_each_poll() {
        let dir = temp("huge");
        for n in 0..10 {
            std::fs::write(dir.join(format!("{n}.bin")), b"x").unwrap();
        }
        assert_eq!(walk_limited(&dir, 4, MAX_FILES).len(), 4);
        assert_eq!(walk_limited(&dir, MAX_ENTRIES, 3).len(), 3);
        assert_eq!(walk(&dir).len(), 10);
        // entries that are not wanted count too: 10 notes and the subfolder come first
        let other = temp("huge-notes");
        for n in 0..10 {
            std::fs::write(other.join(format!("{n}.txt")), b"x").unwrap();
        }
        std::fs::create_dir(other.join("logs")).unwrap();
        std::fs::write(other.join("logs/1.bin"), b"x").unwrap();
        assert!(walk_limited(&other, 11, MAX_FILES).is_empty());
        assert_eq!(walk_limited(&other, 12, MAX_FILES).len(), 1);
        let _ = std::fs::remove_dir_all(dir);
        let _ = std::fs::remove_dir_all(other);
    }

    #[test]
    fn a_file_is_offered_once_it_holds_still_and_the_engine_calls_it_new() {
        let dir = temp("steady");
        let folders = [folder(&dir)];
        let tlog = dir.join("a.tlog");
        std::fs::write(&tlog, b"first").unwrap();
        let mut w = Watcher::default();
        assert!(poll(&mut w, &folders).is_empty()); // just seen
        std::fs::write(&tlog, b"first and more").unwrap(); // still being written
        assert!(poll(&mut w, &folders).is_empty());
        let ready = poll(&mut w, &folders); // held still for a poll
        assert_eq!(ready, std::slice::from_ref(&tlog));
        w.checked(&ready, &[tlog.to_string_lossy().into_owned()]);
        let pending = w.pending();
        assert_eq!(pending.len(), 1);
        assert_eq!(
            (pending[0].name.as_str(), pending[0].station.as_str()),
            ("a.tlog", "Field tablet")
        );
        assert!(poll(&mut w, &folders).is_empty()); // checked: not asked again
        assert_eq!(w.pending().len(), 1);
        // imported elsewhere: the engine no longer calls it new
        let again = w.recheck();
        w.checked(&again, &[]);
        assert!(w.pending().is_empty());
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_file_that_goes_away_or_changes_is_no_longer_offered() {
        let dir = temp("gone");
        let folders = [folder(&dir)];
        let a = dir.join("a.param");
        let b = dir.join("b.param");
        std::fs::write(&a, b"A,1\n").unwrap();
        std::fs::write(&b, b"B,1\n").unwrap();
        let mut w = Watcher::default();
        poll(&mut w, &folders);
        let ready = poll(&mut w, &folders);
        let names: Vec<String> = ready
            .iter()
            .map(|p| p.to_string_lossy().into_owned())
            .collect();
        w.checked(&ready, &names);
        assert_eq!(w.pending().len(), 2);
        std::fs::remove_file(&a).unwrap();
        std::fs::write(&b, b"B,2 and longer\n").unwrap();
        assert!(poll(&mut w, &folders).is_empty());
        assert!(w.pending().is_empty());
        let _ = std::fs::remove_dir_all(dir);
    }
}
