use std::fs;
use std::io::{Read, Write};
use std::net::{IpAddr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

pub const DESCRIPTOR_RELATIVE_PATH: &str = "runtime/service-descriptor.json";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServiceDescriptor {
    pub version: u32,
    pub base_url: String,
    pub token: String,
    pub pid: u32,
    #[serde(default)]
    pub data_dir: String,
    #[serde(default)]
    pub data_dir_id: String,
    #[serde(default)]
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceConnectionInfo {
    pub base_url: String,
    pub token: String,
    pub data_dir: String,
    pub managed: bool,
    pub keep_running_in_background: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShellSettings {
    pub keep_running_in_background: bool,
    pub managed: bool,
    pub data_dir: String,
    pub base_url: Option<String>,
}

pub struct ManagedProcess {
    pub child: Child,
    pub stdin_guard: Option<ChildStdin>,
}

impl ManagedProcess {
    pub fn stop_gracefully(&mut self) {
        // Closing stdin notifies `talaria-data --exit-with-parent` to exit cleanly.
        drop(self.stdin_guard.take());

        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub struct ShellRuntimeState {
    pub data_dir: PathBuf,
    pub descriptor: Option<ServiceDescriptor>,
    pub process: Option<ManagedProcess>,
    pub managed: bool,
    pub keep_running_in_background: bool,
    pub popout_seq: u64,
    pub startup_error: Option<String>,
}

impl ShellRuntimeState {
    pub fn new(data_dir: PathBuf) -> Self {
        Self {
            data_dir,
            descriptor: None,
            process: None,
            managed: false,
            keep_running_in_background: false,
            popout_seq: 0,
            startup_error: None,
        }
    }

    pub fn connection_info(&self) -> Option<ServiceConnectionInfo> {
        let desc = self.descriptor.as_ref()?;
        Some(ServiceConnectionInfo {
            base_url: desc.base_url.clone(),
            token: desc.token.clone(),
            data_dir: self.data_dir.to_string_lossy().into_owned(),
            managed: self.managed,
            keep_running_in_background: self.keep_running_in_background,
        })
    }

    pub fn shell_settings(&self) -> ShellSettings {
        ShellSettings {
            keep_running_in_background: self.keep_running_in_background,
            managed: self.managed,
            data_dir: self.data_dir.to_string_lossy().into_owned(),
            base_url: self.descriptor.as_ref().map(|d| d.base_url.clone()),
        }
    }

    pub fn stop_if_configured(&mut self) {
        if should_stop_on_close(self.keep_running_in_background, self.managed)
            && let Some(mut proc_handle) = self.process.take()
        {
            proc_handle.stop_gracefully();
            self.managed = false;
        }
    }

    pub fn force_stop(&mut self) {
        if let Some(mut proc_handle) = self.process.take() {
            proc_handle.stop_gracefully();
        }
        self.managed = false;
    }
}

pub fn should_stop_on_close(keep_running_in_background: bool, managed: bool) -> bool {
    managed && !keep_running_in_background
}

pub fn default_data_dir() -> PathBuf {
    if let Ok(explicit) = std::env::var("TALARIA_DATA_DIR")
        && !explicit.trim().is_empty()
    {
        return PathBuf::from(explicit);
    }
    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join(".talaria").join("data");
    }
    std::env::temp_dir().join("talaria-data")
}

pub fn descriptor_path(data_dir: &Path) -> PathBuf {
    data_dir.join(DESCRIPTOR_RELATIVE_PATH)
}

/// Strictly validate that `base_url` uses `http://` and binds to a loopback host.
pub fn parse_loopback_host_port(base_url: &str) -> Result<(String, u16), String> {
    let trimmed = base_url.trim().trim_end_matches('/');
    let rest = trimmed
        .strip_prefix("http://")
        .ok_or_else(|| format!("base_url must use http:// scheme: {base_url}"))?;
    if rest.contains('/') || rest.contains('?') || rest.contains('#') || rest.contains('@') {
        return Err(format!("base_url must be origin-only without path/query: {base_url}"));
    }

    let (host, port_str) = if let Some(after_open) = rest.strip_prefix('[') {
        let (ip_part, tail) = after_open
            .split_once(']')
            .ok_or_else(|| format!("invalid IPv6 host in base_url: {base_url}"))?;
        let port_part = tail
            .strip_prefix(':')
            .ok_or_else(|| format!("missing port in base_url: {base_url}"))?;
        (ip_part, port_part)
    } else {
        rest.rsplit_once(':')
            .ok_or_else(|| format!("missing port in base_url: {base_url}"))?
    };

    let port: u16 = port_str
        .parse()
        .map_err(|_| format!("invalid port in base_url: {base_url}"))?;
    if port == 0 {
        return Err(format!("port must be > 0 in base_url: {base_url}"));
    }

    if host != "localhost" {
        let ip: IpAddr = host
            .parse()
            .map_err(|_| format!("non-loopback host rejected: {host}"))?;
        if !ip.is_loopback() {
            return Err(format!("non-loopback IP rejected: {host}"));
        }
    }

    Ok((host.to_string(), port))
}

pub fn read_service_descriptor(data_dir: &Path) -> Result<Option<ServiceDescriptor>, String> {
    let path = descriptor_path(data_dir);
    if !path.is_file() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&path)
        .map_err(|err| format!("failed to read {}: {err}", path.display()))?;
    let desc: ServiceDescriptor = serde_json::from_str(&raw)
        .map_err(|err| format!("failed to parse {}: {err}", path.display()))?;
    if desc.version != 1 {
        return Err(format!("unsupported descriptor version {}", desc.version));
    }
    if desc.token.trim().is_empty() || desc.pid == 0 {
        return Err("descriptor missing valid token or pid".to_string());
    }
    parse_loopback_host_port(&desc.base_url)?;
    Ok(Some(desc))
}

pub fn probe_tcp_health(base_url: &str) -> bool {
    let Ok((host, port)) = parse_loopback_host_port(base_url) else {
        return false;
    };
    let addr: SocketAddr = match host.as_str() {
        "localhost" => SocketAddr::from(([127, 0, 0, 1], port)),
        _ => match host.parse::<IpAddr>() {
            Ok(ip) => SocketAddr::new(ip, port),
            Err(_) => return false,
        },
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(400)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(400)));
    let req = format!("GET /api/health HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 256];
    let Ok(n) = stream.read(&mut buf) else {
        return false;
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
}

pub fn resolve_talaria_data_binary() -> PathBuf {
    if let Ok(explicit) = std::env::var("TALARIA_DATA_BIN")
        && !explicit.trim().is_empty()
    {
        return PathBuf::from(explicit);
    }

    if let Ok(exe) = std::env::current_exe()
        && let Some(dir) = exe.parent()
    {
        let sibling = dir.join(bin_name("talaria-data"));
        if sibling.is_file() {
            return sibling;
        }
    }

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for profile in ["release", "debug"] {
        let candidate = manifest_dir
            .join("..")
            .join("..")
            .join("engine")
            .join("target")
            .join(profile)
            .join(bin_name("talaria-data"));
        if candidate.is_file() {
            return candidate;
        }
    }

    PathBuf::from(bin_name("talaria-data"))
}

fn bin_name(base: &str) -> String {
    if cfg!(windows) {
        format!("{base}.exe")
    } else {
        base.to_string()
    }
}

pub fn bootstrap_script(info: &ServiceConnectionInfo) -> String {
    let payload = serde_json::json!({
        "baseUrl": info.base_url,
        "token": info.token,
    });
    format!("window.__TALARIA_BOOTSTRAP__ = {payload};")
}

pub fn discover_or_spawn_service(state: &mut ShellRuntimeState, desk_dist: Option<&Path>) {
    if let Ok(Some(existing)) = read_service_descriptor(&state.data_dir)
        && probe_tcp_health(&existing.base_url)
    {
        state.descriptor = Some(existing);
        state.managed = false;
        state.startup_error = None;
        return;
    }

    let binary = resolve_talaria_data_binary();
    let mut cmd = Command::new(&binary);
    cmd.arg("up")
        .arg("--data-dir")
        .arg(&state.data_dir)
        .arg("--exit-with-parent")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    if let Ok(pack) = std::env::var("TALARIA_PACK")
        && !pack.trim().is_empty()
    {
        cmd.arg("--pack").arg(pack.trim());
    }

    if let Some(dist) = desk_dist
        && dist.is_dir()
    {
        cmd.arg("--desk-dist").arg(dist);
    }

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(err) => {
            state.startup_error = Some(format!(
                "failed to launch talaria-data ({}): {err}",
                binary.display()
            ));
            return;
        }
    };

    let stdin_guard = child.stdin.take();
    let child_pid = child.id();

    let deadline = Instant::now() + Duration::from_secs(6);
    while Instant::now() < deadline {
        if let Ok(Some(desc)) = read_service_descriptor(&state.data_dir)
            && desc.pid == child_pid
            && probe_tcp_health(&desc.base_url)
        {
            state.descriptor = Some(desc);
            state.process = Some(ManagedProcess { child, stdin_guard });
            state.managed = true;
            state.startup_error = None;
            return;
        }
        if let Ok(Some(status)) = child.try_wait() {
            state.startup_error = Some(format!(
                "talaria-data exited prematurely with status {status}"
            ));
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }

    state.process = Some(ManagedProcess { child, stdin_guard });
    state.managed = true;
    state.startup_error = Some("timed out waiting for talaria-data service-descriptor.json".to_string());
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn validates_strict_loopback_urls() {
        assert_eq!(
            parse_loopback_host_port("http://127.0.0.1:8080"),
            Ok(("127.0.0.1".to_string(), 8080))
        );
        assert_eq!(
            parse_loopback_host_port("http://[::1]:9001/"),
            Ok(("::1".to_string(), 9001))
        );
        assert_eq!(
            parse_loopback_host_port("http://localhost:5173"),
            Ok(("localhost".to_string(), 5173))
        );
        assert!(parse_loopback_host_port("http://0.0.0.0:8080").is_err());
        assert!(parse_loopback_host_port("http://192.168.1.20:8080").is_err());
        assert!(parse_loopback_host_port("https://127.0.0.1:8080").is_err());
        assert!(parse_loopback_host_port("http://127.0.0.1:8080/api").is_err());
    }

    #[test]
    fn reads_and_validates_service_descriptor() {
        let dir = std::env::temp_dir().join(format!("talaria-tauri-test-{}", std::process::id()));
        let runtime = dir.join("runtime");
        fs::create_dir_all(&runtime).unwrap();
        let desc_path = descriptor_path(&dir);
        fs::write(
            &desc_path,
            r#"{
              "version": 1,
              "base_url": "http://127.0.0.1:18442",
              "token": "secret-token-123",
              "pid": 4242,
              "data_dir": "/tmp/talaria",
              "data_dir_id": "abc",
              "created_at": "2026-11-25T00:00:00Z"
            }"#,
        )
        .unwrap();

        let parsed = read_service_descriptor(&dir).unwrap().unwrap();
        assert_eq!(parsed.base_url, "http://127.0.0.1:18442");
        assert_eq!(parsed.token, "secret-token-123");
        assert_eq!(parsed.pid, 4242);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn reuses_running_health_probe_without_spawning() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buf = [0u8; 512];
                let _ = stream.read(&mut buf);
                let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok");
            }
        });

        let dir = std::env::temp_dir().join(format!("talaria-tauri-reuse-{}", std::process::id()));
        let runtime = dir.join("runtime");
        fs::create_dir_all(&runtime).unwrap();
        fs::write(
            descriptor_path(&dir),
            format!(
                r#"{{"version":1,"base_url":"http://127.0.0.1:{port}","token":"tok","pid":9999}}"#
            ),
        )
        .unwrap();

        let mut state = ShellRuntimeState::new(dir.clone());
        discover_or_spawn_service(&mut state, None);
        assert!(!state.managed);
        assert_eq!(
            state.descriptor.as_ref().map(|d| d.token.as_str()),
            Some("tok")
        );

        let _ = handle.join();
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn close_policy_respects_background_toggle_and_ownership() {
        assert!(should_stop_on_close(false, true));
        assert!(!should_stop_on_close(true, true));
        assert!(!should_stop_on_close(false, false));
        assert!(!should_stop_on_close(true, false));
    }
}
