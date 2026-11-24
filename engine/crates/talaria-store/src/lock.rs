//! Exclusive data-directory lock helper using `fs2` (`<data-dir>/runtime/service.lock`).
//!
//! Read-only `ColdReader` instances NEVER acquire this write lock.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use serde::{Deserialize, Serialize};

use crate::error::{Result, StoreError};

const LOCK_SUBDIR: &str = "runtime";
const LOCK_NAME: &str = "service.lock";

/// Metadata recorded inside `<data-dir>/runtime/service.lock`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LockHolder {
    pub pid: Option<u32>,
    pub role: Option<String>,
    pub started_at: Option<String>,
}

/// Compute `<data-dir>/runtime/service.lock` path.
pub fn service_lock_path(data_dir: &Path) -> PathBuf {
    data_dir.join(LOCK_SUBDIR).join(LOCK_NAME)
}

/// Exclusive cross-process file lock on `<data-dir>/runtime/service.lock`.
///
/// Matches Python `ServiceLock`: never deletes the lock file on release/drop to prevent
/// inode races; instead truncates metadata and releases `flock`.
#[derive(Debug)]
pub struct DataDirLock {
    path: PathBuf,
    file: Option<File>,
    holder: Option<LockHolder>,
}

impl DataDirLock {
    /// Acquire the exclusive `fs2` lock on `<data-dir>/runtime/service.lock` without blocking.
    pub fn acquire(data_dir: impl AsRef<Path>, role: impl Into<String>) -> Result<Self> {
        let data_dir = data_dir.as_ref();
        let path = service_lock_path(data_dir);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut opts = OpenOptions::new();
        opts.read(true).write(true).create(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts.open(&path).map_err(|err| {
            StoreError::ServiceLocked(format!(
                "cannot open data-dir lock {}: {err}",
                path.display()
            ))
        })?;

        if let Err(err) = file.try_lock_exclusive() {
            let holder = read_holder_from_file(&mut file);
            return Err(StoreError::ServiceLocked(format!(
                "data directory already locked ({}) by {:?}: {err}",
                path.display(),
                holder
            )));
        }

        let holder = LockHolder {
            pid: Some(std::process::id()),
            role: Some(role.into()),
            started_at: Some(Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)),
        };
        let payload = serde_json::to_vec(&holder)?;
        file.set_len(0)?;
        file.seek(SeekFrom::Start(0))?;
        file.write_all(&payload)?;
        file.sync_data()?;

        Ok(Self {
            path,
            file: Some(file),
            holder: Some(holder),
        })
    }

    /// Non-destructively probe whether `<data-dir>/runtime/service.lock` is currently held.
    pub fn probe(data_dir: impl AsRef<Path>) -> Result<Option<LockHolder>> {
        let path = service_lock_path(data_dir.as_ref());
        if !path.exists() {
            return Ok(None);
        }
        let mut file = match OpenOptions::new().read(true).write(true).open(&path) {
            Ok(f) => f,
            Err(_) => {
                let mut ro = File::open(&path)?;
                return Ok(read_holder_from_file(&mut ro));
            }
        };
        match file.try_lock_exclusive() {
            Ok(()) => {
                let _ = file.unlock();
                Ok(None)
            }
            Err(_) => Ok(read_holder_from_file(&mut file)),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn holder(&self) -> Option<&LockHolder> {
        self.holder.as_ref()
    }

    /// Explicitly truncate metadata and release the `fs2` lock.
    pub fn release(&mut self) {
        if let Some(file) = self.file.take() {
            self.holder = None;
            let _ = file.set_len(0);
            let _ = file.sync_data();
            let _ = file.unlock();
        }
    }
}

impl Drop for DataDirLock {
    fn drop(&mut self) {
        self.release();
    }
}

fn read_holder_from_file(file: &mut File) -> Option<LockHolder> {
    let _ = file.seek(SeekFrom::Start(0));
    let mut raw = String::new();
    if file.read_to_string(&mut raw).is_ok() && !raw.trim().is_empty() {
        serde_json::from_str(&raw).ok()
    } else {
        None
    }
}
