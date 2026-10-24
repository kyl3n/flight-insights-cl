//! Where objects are shared (spec: Shared storage and sync). The sync loop sees a
//! drive only through this trait: `FolderDrive`, a local folder, serves tests and the
//! soak harness, and the Google Drive client (WP19) is the other implementation.
//!
//! A drive only ever gains files. Nothing here deletes or overwrites: an upload to a
//! path that exists is refused, so a second writer can never replace an object.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::sync::sodium;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// Relative, with forward slashes: `devices/<laptop>/<seq>-<key>.fio`.
    pub path: String,
    pub size: u64,
}

pub trait Drive: Send + Sync {
    /// Every file under `prefix` ("devices/", "registry/").
    fn list(&self, prefix: &str) -> io::Result<Vec<Entry>>;
    /// Writes a new file whole and returns the SHA-256 the drive holds for it.
    /// A path that exists is an error: objects are never replaced.
    fn upload(&self, path: &str, from: &Path) -> io::Result<[u8; 32]>;
    fn download(&self, path: &str, to: &Path) -> io::Result<()>;
}

/// A drive in a local folder.
pub struct FolderDrive {
    root: PathBuf,
}

impl FolderDrive {
    pub fn new(root: impl Into<PathBuf>) -> io::Result<FolderDrive> {
        let root = root.into();
        fs::create_dir_all(&root)?;
        Ok(FolderDrive { root })
    }

    fn local(&self, path: &str) -> io::Result<PathBuf> {
        let ok = !path.is_empty()
            && path.split('/').all(|p| {
                !p.is_empty() && p != "." && p != ".." && !p.contains('\\') && !p.contains(':')
            });
        if !ok {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "not a drive path",
            ));
        }
        Ok(self.root.join(path))
    }

    fn walk(&self, dir: &Path, out: &mut Vec<Entry>) -> io::Result<()> {
        if !dir.is_dir() {
            return Ok(());
        }
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            if path.is_dir() {
                self.walk(&path, out)?;
            } else if !entry.file_name().to_string_lossy().starts_with('.') {
                let rel = path.strip_prefix(&self.root).expect("under the root");
                let rel: Vec<String> = rel
                    .components()
                    .map(|c| c.as_os_str().to_string_lossy().into_owned())
                    .collect();
                out.push(Entry {
                    path: rel.join("/"),
                    size: entry.metadata()?.len(),
                });
            }
        }
        Ok(())
    }
}

impl Drive for FolderDrive {
    fn list(&self, prefix: &str) -> io::Result<Vec<Entry>> {
        let mut out = Vec::new();
        self.walk(&self.root, &mut out)?;
        out.retain(|e| e.path.starts_with(prefix));
        out.sort_by(|a, b| a.path.cmp(&b.path));
        Ok(out)
    }

    fn upload(&self, path: &str, from: &Path) -> io::Result<[u8; 32]> {
        let target = self.local(path)?;
        if target.exists() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "an object is never replaced",
            ));
        }
        fs::create_dir_all(target.parent().expect("a drive path has a folder"))?;
        // Written beside it under a hidden name, then renamed: a reader never sees half
        // a file, as a resumable upload appears only once complete.
        let partial = target.with_file_name(format!(
            ".{}.partial",
            hex::encode(sodium::random::<8>().as_ref())
        ));
        fs::copy(from, &partial)?;
        if target.exists() {
            fs::remove_file(&partial)?;
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "an object is never replaced",
            ));
        }
        fs::rename(&partial, &target)?;
        sodium::Sha256::of_file(&target)
    }

    fn download(&self, path: &str, to: &Path) -> io::Result<()> {
        fs::copy(self.local(path)?, to).map(|_| ())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "fi-drive-{name}-{}",
            hex::encode(sodium::random::<6>().as_ref())
        ))
    }

    #[test]
    fn a_folder_drive_only_ever_gains_files() {
        let root = temp("root");
        let drive = FolderDrive::new(&root).unwrap();
        let src = temp("src");
        fs::write(&src, b"object").unwrap();
        let sum = drive
            .upload("devices/ab/000000000001-cd.fio", &src)
            .unwrap();
        assert_eq!(sum, sodium::sha256(b"object"));
        let again = drive
            .upload("devices/ab/000000000001-cd.fio", &src)
            .unwrap_err();
        assert_eq!(again.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(
            drive.list("devices/").unwrap(),
            vec![Entry {
                path: "devices/ab/000000000001-cd.fio".into(),
                size: 6
            }]
        );
        assert!(drive.list("registry/").unwrap().is_empty());
        let out = temp("out");
        drive
            .download("devices/ab/000000000001-cd.fio", &out)
            .unwrap();
        assert_eq!(fs::read(&out).unwrap(), b"object");
        for bad in ["../x", "devices/../../x", "", "a//b", "a\\b", "C:/x"] {
            assert!(drive.upload(bad, &src).is_err(), "{bad}");
        }
        for p in [&root, &src, &out] {
            let _ = fs::remove_dir_all(p);
            let _ = fs::remove_file(p);
        }
    }
}
