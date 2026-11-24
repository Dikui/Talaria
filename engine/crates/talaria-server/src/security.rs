//! Loopback enforcement, `0600` service descriptor, Bearer token auth, and `X-Talaria-Data-Dir` guard.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::IpAddr;
use std::path::{Path, PathBuf};

use axum::extract::{Request, State};
use axum::http::{HeaderMap, Method};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use chrono::{SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use talaria_contracts::error_codes;
use uuid::Uuid;

use crate::error::ApiError;

pub const DESCRIPTOR_NAME: &str = "service-descriptor.json";
pub const DESCRIPTOR_VERSION: u32 = 1;
pub const DATA_DIR_HEADER: &str = "x-talaria-data-dir";
pub const NOW_MS_HEADER: &str = "x-talaria-now-ms";
pub const BARS_MANIFEST_HEADER: &str = "x-talaria-bars-manifest";
pub const BARS_HOT_REVISION_HEADER: &str = "x-talaria-bars-hot-revision";
pub const BARS_HOT_REVISION_UNSTABLE: &str = "unstable";

/// Feature toggles matching Python `service.json` & `service-descriptor.json`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct FeatureToggles {
    pub market_data: bool,
    pub cold_store: bool,
    pub alerts: bool,
    pub scanner: bool,
}

impl Default for FeatureToggles {
    fn default() -> Self {
        Self {
            market_data: true,
            cold_store: true,
            alerts: true,
            scanner: true,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ServiceConfigFile {
    #[serde(default = "default_config_version")]
    version: u32,
    #[serde(default)]
    features: PartialFeatureToggles,
    #[serde(default)]
    #[allow(dead_code)]
    comment: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct PartialFeatureToggles {
    market_data: Option<bool>,
    cold_store: Option<bool>,
    alerts: Option<bool>,
    scanner: Option<bool>,
}

fn default_config_version() -> u32 {
    1
}

/// Load `<data-dir>/service.json` if present (defaults all features enabled when missing).
pub fn load_service_features(data_dir: &Path) -> Result<FeatureToggles, ApiError> {
    let path = data_dir.join("service.json");
    if !path.exists() {
        return Ok(FeatureToggles::default());
    }
    let raw = fs::read_to_string(&path).map_err(|err| {
        ApiError::validation(format!("cannot read service.json {}: {err}", path.display()))
    })?;
    let parsed: ServiceConfigFile = serde_json::from_str(&raw).map_err(|err| {
        ApiError::validation(format!("invalid service.json {}: {err}", path.display()))
    })?;
    if parsed.version != 1 {
        return Err(ApiError::validation(format!(
            "unsupported service.json version {} in {}",
            parsed.version,
            path.display()
        )));
    }
    Ok(FeatureToggles {
        market_data: parsed.features.market_data.unwrap_or(true),
        cold_store: parsed.features.cold_store.unwrap_or(true),
        alerts: parsed.features.alerts.unwrap_or(true),
        scanner: parsed.features.scanner.unwrap_or(true),
    })
}

/// On-disk service descriptor (`<data-dir>/runtime/service-descriptor.json`) compatible with
/// Python `talaria.service_endpoint` and Tauri v2 sidecar discovery.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServiceDescriptor {
    pub version: u32,
    pub base_url: String,
    pub token: String,
    pub pid: u32,
    pub data_dir: String,
    pub data_dir_id: String,
    pub features: FeatureToggles,
    pub created_at: String,
}

/// Resolve canonical path even if target directory does not yet exist.
pub fn resolve_data_dir(data_dir: &Path) -> std::io::Result<PathBuf> {
    fs::create_dir_all(data_dir)?;
    fs::canonicalize(data_dir)
}

/// Compute SHA-256 hex identifier of canonicalized `data_dir`, matching Python `data_dir_id`.
pub fn data_dir_id(data_dir: &Path) -> String {
    let resolved = fs::canonicalize(data_dir).unwrap_or_else(|_| data_dir.to_path_buf());
    let digest = Sha256::digest(resolved.to_string_lossy().as_bytes());
    hex::encode(digest)
}

/// Compute `<data-dir>/runtime/service-descriptor.json`.
pub fn service_descriptor_path(data_dir: &Path) -> PathBuf {
    data_dir.join("runtime").join(DESCRIPTOR_NAME)
}

/// Validate that a host string is strictly a loopback IP address (`127.0.0.1` / `::1`).
pub fn validate_loopback_host(host: &str) -> Result<IpAddr, ApiError> {
    let ip: IpAddr = host.parse().map_err(|_| {
        ApiError::validation(format!("host must be a loopback IP address: {host}"))
    })?;
    if !ip.is_loopback() {
        return Err(ApiError::validation(format!(
            "refusing non-loopback bind host: {host}"
        )));
    }
    Ok(ip)
}

/// Format HTTP base URL for a loopback IP and bound port.
pub fn loopback_base_url(ip: IpAddr, port: u16) -> String {
    match ip {
        IpAddr::V4(addr) => format!("http://{addr}:{port}"),
        IpAddr::V6(addr) => format!("http://[{addr}]:{port}"),
    }
}

/// Generate a cryptographically random 64-char hex Bearer token via OS CSPRNG.
pub fn generate_bearer_token() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}

/// Constant-time byte-slice comparison for Bearer token validation.
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (&a, &b) in left.iter().zip(right.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

/// Atomically write `service-descriptor.json` with `0700` runtime dir and `0600` file permissions.
pub fn write_service_descriptor(
    data_dir: &Path,
    descriptor: &ServiceDescriptor,
) -> std::io::Result<PathBuf> {
    let dest = service_descriptor_path(data_dir);
    let parent = dest
        .parent()
        .expect("service-descriptor path always has runtime parent");
    fs::create_dir_all(parent)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    }

    let payload = serde_json::to_string_pretty(descriptor)
        .map_err(std::io::Error::other)?
        + "\n";
    let tmp = parent.join(format!(
        ".{DESCRIPTOR_NAME}.{}.{}.tmp",
        std::process::id(),
        Uuid::new_v4().simple()
    ));

    let write_res = (|| -> std::io::Result<()> {
        let mut opts = OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts.open(&tmp)?;
        file.write_all(payload.as_bytes())?;
        file.flush()?;
        file.sync_all()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600))?;
        }
        drop(file);
        fs::rename(&tmp, &dest)?;
        Ok(())
    })();

    if write_res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    write_res?;
    Ok(dest)
}

/// Remove `service-descriptor.json` on clean daemon shutdown (best-effort).
pub fn remove_service_descriptor(data_dir: &Path) {
    let _ = fs::remove_file(service_descriptor_path(data_dir));
}

/// Build a `ServiceDescriptor` timestamped now in RFC-3339 UTC seconds.
pub fn build_descriptor(
    base_url: String,
    token: String,
    data_dir: &Path,
    data_dir_id: String,
    features: FeatureToggles,
) -> ServiceDescriptor {
    ServiceDescriptor {
        version: DESCRIPTOR_VERSION,
        base_url,
        token,
        pid: std::process::id(),
        data_dir: data_dir.to_string_lossy().into_owned(),
        data_dir_id,
        features,
        created_at: Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
    }
}

/// Constant-time verification of `Authorization: Bearer <token>` against `expected_token`.
pub fn verify_bearer_header(headers: &HeaderMap, expected_token: &str) -> bool {
    let Some(raw) = headers.get(axum::http::header::AUTHORIZATION) else {
        return false;
    };
    let Ok(text) = raw.to_str() else {
        return false;
    };
    let Some((scheme, credential)) = text.split_once(' ') else {
        return false;
    };
    scheme.eq_ignore_ascii_case("bearer")
        && !credential.is_empty()
        && constant_time_eq(credential.as_bytes(), expected_token.as_bytes())
}

/// Shared security state for Axum middleware.
#[derive(Debug, Clone)]
pub struct SecurityContext {
    pub token: String,
    pub data_dir: PathBuf,
    pub data_dir_id: String,
}

impl SecurityContext {
    /// Extract Bearer token from `Authorization` header and verify in constant time.
    pub fn check_bearer(&self, headers: &HeaderMap) -> Result<(), ApiError> {
        let Some(raw) = headers.get(axum::http::header::AUTHORIZATION) else {
            return Err(ApiError::unauthorized("写操作需要有效的 Bearer token"));
        };
        let Ok(text) = raw.to_str() else {
            return Err(ApiError::unauthorized("无效的 Authorization 头"));
        };
        let Some((scheme, credential)) = text.split_once(' ') else {
            return Err(ApiError::unauthorized("写操作需要有效的 Bearer token"));
        };
        if !scheme.eq_ignore_ascii_case("bearer")
            || credential.is_empty()
            || !constant_time_eq(credential.as_bytes(), self.token.as_bytes())
        {
            return Err(ApiError::unauthorized("写操作需要有效的 Bearer token"));
        }
        Ok(())
    }

    /// Enforce `X-Talaria-Data-Dir` header guard when sent by a client.
    pub fn check_data_dir_header(&self, headers: &HeaderMap) -> Result<(), ApiError> {
        let Some(raw) = headers.get(DATA_DIR_HEADER) else {
            return Ok(());
        };
        let Ok(value) = raw.to_str() else {
            return Err(ApiError::validation_code(
                error_codes::VALIDATION_ERROR,
                "invalid X-Talaria-Data-Dir header encoding",
            ));
        };
        if value == self.data_dir_id {
            return Ok(());
        }
        // Also accept exact canonicalized path string when a client passes the directory path directly.
        let candidate = Path::new(value);
        if candidate == self.data_dir
            || fs::canonicalize(candidate).is_ok_and(|p| p == self.data_dir)
        {
            return Ok(());
        }
        Err(ApiError::unauthorized(format!(
            "X-Talaria-Data-Dir mismatch: expected {} ({})",
            self.data_dir_id,
            self.data_dir.display()
        )))
    }
}

fn is_write_method(method: &Method) -> bool {
    matches!(
        *method,
        Method::POST | Method::PUT | Method::PATCH | Method::DELETE
    )
}

/// Axum middleware enforcing `X-Talaria-Data-Dir` when sent and Bearer token auth on `/api/*` writes.
pub async fn security_middleware(
    State(security): State<SecurityContext>,
    request: Request,
    next: Next,
) -> Response {
    if let Err(err) = security.check_data_dir_header(request.headers()) {
        return err.into_response();
    }
    let path = request.uri().path();
    if path.starts_with("/api") && is_write_method(request.method())
        && let Err(err) = security.check_bearer(request.headers())
    {
        return err.into_response();
    }
    next.run(request).await
}
