use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

pub const SCHEMES_DIR_NAME: &str = "schemes";
pub const LOGS_DIR_NAME: &str = "logs";
pub const SETTINGS_RELATIVE_PATH: &str = "runtime/workbench-settings.json";

pub const ERROR_LOCK_CONFLICT: &str = "LOCK_CONFLICT";
pub const ERROR_ALREADY_RUNNING: &str = "ALREADY_RUNNING";
pub const ERROR_VALIDATION_FAILED: &str = "VALIDATION_FAILED";
pub const ERROR_LAUNCH_FAILED: &str = "LAUNCH_FAILED";
pub const ERROR_CRASHED: &str = "CRASHED";
pub const ERROR_RUNTIME_NOT_FOUND: &str = "RUNTIME_NOT_FOUND";

const DEFAULT_GRACEFUL_TIMEOUT: Duration = Duration::from_secs(5);
const DEFAULT_STARTUP_PROBE: Duration = Duration::from_millis(450);

const PYTHON_BATCH_VALIDATE_SCRIPT: &str = r#"
import json, sys
from pathlib import Path

data_dir = Path(sys.argv[1])
paths = [Path(p) for p in sys.argv[2:]]
results = []

try:
    from talaria.account.host import _prepare_files, default_registry, resolve_run_config
    from talaria.account.scheme import Slot
except Exception as exc:
    for p in paths:
        results.append({
            "path": str(p),
            "valid": False,
            "scheme": p.stem,
            "sub_accounts": [],
            "error": f"Failed to import talaria runtime: {exc}",
        })
    print(json.dumps(results, ensure_ascii=False))
    sys.exit(0)

registry = default_registry()

for p in paths:
    scheme_name = p.stem
    sub_accounts = []
    try:
        cfg = resolve_run_config(p, data_dir)
        scheme_name = cfg.scheme.scheme
        sub_accounts = list(cfg.scheme.sub_accounts)
        files = _prepare_files(cfg, "paper")
        for slot in (Slot.DATA, Slot.STRATEGY, Slot.SIZING, Slot.RISK, Slot.FILL):
            if slot in files:
                continue
            sel = cfg.scheme.slots.selection(slot, "paper")
            registry.resolve(
                slot,
                sel.reference,
                cfg.scheme.interface_version,
                cfg.scheme_dir,
                mode="paper",
            )
        results.append({
            "path": str(p),
            "valid": True,
            "scheme": scheme_name,
            "sub_accounts": sub_accounts,
            "error": None,
        })
    except Exception as exc:
        results.append({
            "path": str(p),
            "valid": False,
            "scheme": scheme_name,
            "sub_accounts": sub_accounts,
            "error": str(exc) or type(exc).__name__,
        })

print(json.dumps(results, ensure_ascii=False))
"#;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ManagedProcessState {
    Running,
    Stopped,
    Crashed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperSchemeEntry {
    pub scheme: String,
    pub path: String,
    pub file_name: String,
    pub sub_accounts: Vec<String>,
    pub valid: bool,
    pub validation_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperSchemeList {
    pub schemes_dir: String,
    pub data_dir: String,
    pub schemes: Vec<PaperSchemeEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaperProcessStatus {
    pub scheme: String,
    pub scheme_path: String,
    pub sub_accounts: Vec<String>,
    pub state: ManagedProcessState,
    pub pid: Option<u32>,
    pub log_path: String,
    pub started_at_ms: Option<u64>,
    pub exited_at_ms: Option<u64>,
    pub exit_code: Option<i32>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct PersistedWorkbenchSettings {
    #[serde(default = "default_settings_version")]
    version: u32,
    #[serde(default)]
    keep_paper_accounts_running: bool,
}

fn default_settings_version() -> u32 {
    1
}

impl Default for PersistedWorkbenchSettings {
    fn default() -> Self {
        Self {
            version: 1,
            keep_paper_accounts_running: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PythonRuntime {
    UvProject {
        uv_bin: PathBuf,
        project_dir: PathBuf,
    },
    TalariaBin {
        talaria_bin: PathBuf,
        python_bin: Option<PathBuf>,
    },
    CustomCommand {
        program: PathBuf,
        args_prefix: Vec<String>,
        cwd: Option<PathBuf>,
    },
}

impl PythonRuntime {
    pub fn discover() -> Option<Self> {
        if let Ok(explicit_uv) = std::env::var("TALARIA_UV_BIN")
            && !explicit_uv.trim().is_empty()
        {
            let uv_bin = PathBuf::from(explicit_uv.trim());
            if let Some(project_dir) = find_project_dir() {
                return Some(Self::UvProject { uv_bin, project_dir });
            }
        }

        let project_dir = find_project_dir();
        if let (Some(uv_bin), Some(project_dir)) = (find_executable("uv"), project_dir.clone()) {
            return Some(Self::UvProject { uv_bin, project_dir });
        }

        if let Some(talaria_bin) = find_talaria_binary() {
            let python_bin = find_executable("python3").or_else(|| find_executable("python"));
            return Some(Self::TalariaBin {
                talaria_bin,
                python_bin,
            });
        }

        None
    }

    fn build_run_command(&self, scheme_path: &Path, data_dir: &Path) -> Command {
        match self {
            Self::UvProject {
                uv_bin,
                project_dir,
            } => {
                let mut cmd = Command::new(uv_bin);
                cmd.current_dir(project_dir)
                    .arg("run")
                    .arg("--project")
                    .arg(project_dir)
                    .arg("talaria")
                    .arg("account")
                    .arg("run")
                    .arg(scheme_path)
                    .arg("--mode")
                    .arg("paper")
                    .arg("--data-dir")
                    .arg(data_dir);
                augment_path_env(&mut cmd);
                cmd
            }
            Self::TalariaBin { talaria_bin, .. } => {
                let mut cmd = Command::new(talaria_bin);
                cmd.arg("account")
                    .arg("run")
                    .arg(scheme_path)
                    .arg("--mode")
                    .arg("paper")
                    .arg("--data-dir")
                    .arg(data_dir);
                augment_path_env(&mut cmd);
                cmd
            }
            Self::CustomCommand {
                program,
                args_prefix,
                cwd,
            } => {
                let mut cmd = Command::new(program);
                if let Some(dir) = cwd {
                    cmd.current_dir(dir);
                }
                for arg in args_prefix {
                    cmd.arg(arg);
                }
                cmd.arg(scheme_path).arg("--mode").arg("paper").arg("--data-dir").arg(data_dir);
                cmd
            }
        }
    }

    fn build_validate_command(&self, data_dir: &Path, paths: &[PathBuf]) -> Option<Command> {
        match self {
            Self::UvProject {
                uv_bin,
                project_dir,
            } => {
                let mut cmd = Command::new(uv_bin);
                cmd.current_dir(project_dir)
                    .arg("run")
                    .arg("--project")
                    .arg(project_dir)
                    .arg("python")
                    .arg("-c")
                    .arg(PYTHON_BATCH_VALIDATE_SCRIPT)
                    .arg(data_dir);
                for p in paths {
                    cmd.arg(p);
                }
                augment_path_env(&mut cmd);
                Some(cmd)
            }
            Self::TalariaBin {
                python_bin: Some(python),
                ..
            } => {
                let mut cmd = Command::new(python);
                cmd.arg("-c")
                    .arg(PYTHON_BATCH_VALIDATE_SCRIPT)
                    .arg(data_dir);
                for p in paths {
                    cmd.arg(p);
                }
                augment_path_env(&mut cmd);
                Some(cmd)
            }
            Self::TalariaBin {
                python_bin: None, ..
            }
            | Self::CustomCommand { .. } => None,
        }
    }
}

struct TrackedAccountProcess {
    scheme: String,
    scheme_path: PathBuf,
    sub_accounts: Vec<String>,
    state: ManagedProcessState,
    child: Option<Child>,
    pid: Option<u32>,
    log_path: PathBuf,
    log_start_offset: u64,
    started_at_ms: Option<u64>,
    exited_at_ms: Option<u64>,
    exit_code: Option<i32>,
    error_code: Option<String>,
    error_message: Option<String>,
}

impl TrackedAccountProcess {
    fn to_status(&self) -> PaperProcessStatus {
        PaperProcessStatus {
            scheme: self.scheme.clone(),
            scheme_path: self.scheme_path.to_string_lossy().into_owned(),
            sub_accounts: self.sub_accounts.clone(),
            state: self.state.clone(),
            pid: self.pid,
            log_path: self.log_path.to_string_lossy().into_owned(),
            started_at_ms: self.started_at_ms,
            exited_at_ms: self.exited_at_ms,
            exit_code: self.exit_code,
            error_code: self.error_code.clone(),
            error_message: self.error_message.clone(),
        }
    }
}

pub struct AccountProcessManager {
    data_dir: PathBuf,
    schemes_dir: PathBuf,
    runtime: Option<PythonRuntime>,
    keep_alive_on_exit: bool,
    graceful_timeout: Duration,
    startup_probe: Duration,
    processes: HashMap<PathBuf, TrackedAccountProcess>,
}

impl AccountProcessManager {
    pub fn new(data_dir: PathBuf) -> Self {
        let schemes_dir = resolve_schemes_dir(&data_dir);
        let settings = load_workbench_settings(&data_dir);
        Self {
            data_dir,
            schemes_dir,
            runtime: PythonRuntime::discover(),
            keep_alive_on_exit: settings.keep_paper_accounts_running,
            graceful_timeout: DEFAULT_GRACEFUL_TIMEOUT,
            startup_probe: DEFAULT_STARTUP_PROBE,
            processes: HashMap::new(),
        }
    }

    pub fn with_runtime(
        data_dir: PathBuf,
        schemes_dir: PathBuf,
        runtime: Option<PythonRuntime>,
    ) -> Self {
        let settings = load_workbench_settings(&data_dir);
        Self {
            data_dir,
            schemes_dir,
            runtime,
            keep_alive_on_exit: settings.keep_paper_accounts_running,
            graceful_timeout: DEFAULT_GRACEFUL_TIMEOUT,
            startup_probe: DEFAULT_STARTUP_PROBE,
            processes: HashMap::new(),
        }
    }

    pub fn set_timeouts(&mut self, graceful_timeout: Duration, startup_probe: Duration) {
        self.graceful_timeout = graceful_timeout;
        self.startup_probe = startup_probe;
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn schemes_dir(&self) -> &Path {
        &self.schemes_dir
    }

    pub fn keep_alive_on_exit(&self) -> bool {
        self.keep_alive_on_exit
    }

    pub fn set_keep_alive_on_exit(&mut self, keep_alive: bool) -> Result<bool, String> {
        self.keep_alive_on_exit = keep_alive;
        save_workbench_settings(
            &self.data_dir,
            &PersistedWorkbenchSettings {
                version: 1,
                keep_paper_accounts_running: keep_alive,
            },
        )?;
        Ok(self.keep_alive_on_exit)
    }

    pub fn list_schemes(&self) -> Result<PaperSchemeList, String> {
        let yaml_files = scan_scheme_files(&self.schemes_dir)?;
        let schemes = if yaml_files.is_empty() {
            Vec::new()
        } else {
            self.validate_scheme_files(&yaml_files)
        };
        Ok(PaperSchemeList {
            schemes_dir: self.schemes_dir.to_string_lossy().into_owned(),
            data_dir: self.data_dir.to_string_lossy().into_owned(),
            schemes,
        })
    }

    pub fn poll_processes(&mut self) {
        for tracked in self.processes.values_mut() {
            if tracked.state != ManagedProcessState::Running {
                continue;
            }
            let Some(child) = tracked.child.as_mut() else {
                continue;
            };
            match child.try_wait() {
                Ok(Some(exit_status)) => {
                    tracked.child = None;
                    tracked.exited_at_ms = Some(now_ms());
                    tracked.exit_code = exit_status.code();
                    if exit_status.success() {
                        tracked.state = ManagedProcessState::Stopped;
                        tracked.error_code = None;
                        tracked.error_message = None;
                    } else {
                        let tail = read_log_tail_since(&tracked.log_path, tracked.log_start_offset);
                        let (err_code, err_msg) = classify_exit_error(&tail, exit_status.code());
                        tracked.state = ManagedProcessState::Crashed;
                        tracked.error_code = Some(err_code);
                        tracked.error_message = Some(err_msg);
                    }
                }
                Ok(None) => {}
                Err(err) => {
                    tracked.child = None;
                    tracked.state = ManagedProcessState::Crashed;
                    tracked.exited_at_ms = Some(now_ms());
                    tracked.error_code = Some(ERROR_CRASHED.to_string());
                    tracked.error_message = Some(format!("failed to poll process status: {err}"));
                }
            }
        }
    }

    pub fn list_processes(&mut self) -> Vec<PaperProcessStatus> {
        self.poll_processes();
        let mut items: Vec<PaperProcessStatus> =
            self.processes.values().map(TrackedAccountProcess::to_status).collect();
        items.sort_by(|a, b| a.scheme.cmp(&b.scheme).then_with(|| a.scheme_path.cmp(&b.scheme_path)));
        items
    }

    pub fn start_scheme(&mut self, raw_scheme_path: &Path) -> Result<PaperProcessStatus, String> {
        self.poll_processes();

        let scheme_path = canonicalize_or_abs(raw_scheme_path);
        if !scheme_path.is_file() {
            return Err(format!(
                "交易方案文件不存在 / Scheme file not found: {}",
                scheme_path.display()
            ));
        }

        // Single-instance guard per scheme.
        if let Some(existing) = self.processes.get(&scheme_path)
            && existing.state == ManagedProcessState::Running
        {
            return Err(format!(
                "[{ERROR_ALREADY_RUNNING}] 该交易方案已在运行中 / Trading scheme is already running: {} (PID {})",
                existing.scheme,
                existing.pid.unwrap_or_default()
            ));
        }

        // Pre-launch scheme validation (including agent strategy approval check when Python runtime is available).
        let entry = self
            .validate_scheme_files(std::slice::from_ref(&scheme_path))
            .into_iter()
            .next()
            .unwrap_or_else(|| fallback_parse_scheme_entry(&scheme_path, None));

        if !entry.valid {
            let reason = entry
                .validation_error
                .unwrap_or_else(|| "scheme validation failed".to_string());
            let log_path = log_path_for_scheme(&self.data_dir, &entry.scheme, &scheme_path);
            let failed = TrackedAccountProcess {
                scheme: entry.scheme.clone(),
                scheme_path: scheme_path.clone(),
                sub_accounts: entry.sub_accounts.clone(),
                state: ManagedProcessState::Failed,
                child: None,
                pid: None,
                log_path,
                log_start_offset: 0,
                started_at_ms: None,
                exited_at_ms: Some(now_ms()),
                exit_code: None,
                error_code: Some(ERROR_VALIDATION_FAILED.to_string()),
                error_message: Some(reason.clone()),
            };
            self.processes.insert(scheme_path, failed);
            return Err(format!(
                "[{ERROR_VALIDATION_FAILED}] 交易方案校验失败 / Scheme validation failed: {reason}"
            ));
        }

        // Check in-process sub-account overlap before spawning.
        for running in self.processes.values() {
            if running.state != ManagedProcessState::Running {
                continue;
            }
            for sub in &entry.sub_accounts {
                if running.sub_accounts.contains(sub) {
                    let msg = format_lock_conflict_message(&format!(
                        "子账户 {sub} 已被当前工作台运行中的方案 {} (PID {}) 占用",
                        running.scheme,
                        running.pid.unwrap_or_default()
                    ));
                    return Err(format!("[{ERROR_LOCK_CONFLICT}] {msg}"));
                }
            }
        }

        let runtime = self.runtime.as_ref().ok_or_else(|| {
            format!(
                "[{ERROR_RUNTIME_NOT_FOUND}] 未找到 uv 或 talaria Python 运行时，无法启动模拟盘账户进程 / Could not locate uv or talaria Python runtime"
            )
        })?;

        let log_path = log_path_for_scheme(&self.data_dir, &entry.scheme, &scheme_path);
        let (stdout_file, stderr_file, log_start_offset) =
            open_log_files(&log_path, &entry.scheme, &scheme_path)?;

        let mut cmd = runtime.build_run_command(&scheme_path, &self.data_dir);
        cmd.stdin(Stdio::null())
            .stdout(Stdio::from(stdout_file))
            .stderr(Stdio::from(stderr_file));

        #[cfg(unix)]
        {
            // Place each account process in its own process group so:
            // 1. SIGINT/SIGTERM/SIGKILL can reach both `uv` and the underlying Python interpreter.
            // 2. Keeping it running after workbench exit isolates it from parent group signals.
            cmd.process_group(0);
        }

        let mut child = cmd.spawn().map_err(|err| {
            format!(
                "[{ERROR_LAUNCH_FAILED}] 无法启动模拟盘账户进程 / Failed to spawn paper account process: {err}"
            )
        })?;

        let pid = child.id();
        let started_at_ms = now_ms();

        // Short startup probe catches immediate failures (lock conflicts, missing deps, bad plugins).
        let probe_deadline = Instant::now() + self.startup_probe;
        while Instant::now() < probe_deadline {
            match child.try_wait() {
                Ok(Some(exit_status)) => {
                    let tail = read_log_tail_since(&log_path, log_start_offset);
                    if exit_status.success() {
                        let stopped = TrackedAccountProcess {
                            scheme: entry.scheme,
                            scheme_path: scheme_path.clone(),
                            sub_accounts: entry.sub_accounts,
                            state: ManagedProcessState::Stopped,
                            child: None,
                            pid: Some(pid),
                            log_path,
                            log_start_offset,
                            started_at_ms: Some(started_at_ms),
                            exited_at_ms: Some(now_ms()),
                            exit_code: exit_status.code(),
                            error_code: None,
                            error_message: None,
                        };
                        let status = stopped.to_status();
                        self.processes.insert(scheme_path, stopped);
                        return Ok(status);
                    }
                    let (err_code, err_msg) = classify_exit_error(&tail, exit_status.code());
                    let failed = TrackedAccountProcess {
                        scheme: entry.scheme,
                        scheme_path: scheme_path.clone(),
                        sub_accounts: entry.sub_accounts,
                        state: ManagedProcessState::Failed,
                        child: None,
                        pid: Some(pid),
                        log_path,
                        log_start_offset,
                        started_at_ms: Some(started_at_ms),
                        exited_at_ms: Some(now_ms()),
                        exit_code: exit_status.code(),
                        error_code: Some(err_code.clone()),
                        error_message: Some(err_msg.clone()),
                    };
                    self.processes.insert(scheme_path, failed);
                    return Err(format!("[{err_code}] {err_msg}"));
                }
                Ok(None) => thread::sleep(Duration::from_millis(35)),
                Err(_) => break,
            }
        }

        let tracked = TrackedAccountProcess {
            scheme: entry.scheme,
            scheme_path: scheme_path.clone(),
            sub_accounts: entry.sub_accounts,
            state: ManagedProcessState::Running,
            child: Some(child),
            pid: Some(pid),
            log_path,
            log_start_offset,
            started_at_ms: Some(started_at_ms),
            exited_at_ms: None,
            exit_code: None,
            error_code: None,
            error_message: None,
        };
        let status = tracked.to_status();
        self.processes.insert(scheme_path, tracked);
        Ok(status)
    }

    pub fn stop_scheme(&mut self, raw_scheme_path: &Path) -> Result<PaperProcessStatus, String> {
        self.poll_processes();
        let scheme_path = canonicalize_or_abs(raw_scheme_path);
        let tracked = self.processes.get_mut(&scheme_path).ok_or_else(|| {
            format!(
                "未找到托管中的模拟盘进程 / No managed paper process found for {}",
                scheme_path.display()
            )
        })?;

        if let Some(mut child) = tracked.child.take() {
            let exit_code = stop_child_gracefully(&mut child, self.graceful_timeout);
            tracked.state = ManagedProcessState::Stopped;
            tracked.exited_at_ms = Some(now_ms());
            tracked.exit_code = exit_code;
            tracked.error_code = None;
            tracked.error_message = None;
        } else if tracked.state == ManagedProcessState::Running {
            tracked.state = ManagedProcessState::Stopped;
            tracked.exited_at_ms = Some(now_ms());
        }

        Ok(tracked.to_status())
    }

    pub fn resolve_log_path(&self, raw_scheme_or_log_path: &Path) -> Result<PathBuf, String> {
        let logs_dir = self.data_dir.join(LOGS_DIR_NAME);
        let candidate = if let Some(tracked) =
            self.processes.get(&canonicalize_or_abs(raw_scheme_or_log_path))
        {
            tracked.log_path.clone()
        } else if raw_scheme_or_log_path.extension().and_then(|e| e.to_str()) == Some("log") {
            raw_scheme_or_log_path.to_path_buf()
        } else {
            let scheme_path = canonicalize_or_abs(raw_scheme_or_log_path);
            let parsed = fallback_parse_scheme_entry(&scheme_path, None);
            log_path_for_scheme(&self.data_dir, &parsed.scheme, &scheme_path)
        };

        if !candidate.is_file() {
            return Err(format!(
                "日志文件尚未生成 / Log file does not exist yet: {}",
                candidate.display()
            ));
        }

        let canonical_logs_dir = canonicalize_or_abs(&logs_dir);
        let canonical_candidate = canonicalize_or_abs(&candidate);
        if !canonical_candidate.starts_with(&canonical_logs_dir) {
            return Err(format!(
                "拒绝打开数据日志目录之外的文件 / Refusing to open file outside logs directory: {}",
                canonical_candidate.display()
            ));
        }

        Ok(canonical_candidate)
    }

    pub fn open_log(&self, raw_scheme_or_log_path: &Path) -> Result<String, String> {
        let path = self.resolve_log_path(raw_scheme_or_log_path)?;
        open_path_in_os(&path)?;
        Ok(path.to_string_lossy().into_owned())
    }

    /// Invoked on workbench exit/close (spec D9).
    /// Returns the PIDs of processes that were intentionally left running in background (when keep-alive is true).
    pub fn on_workbench_exit(&mut self) -> Vec<u32> {
        self.poll_processes();
        let mut kept_pids = Vec::new();
        if self.keep_alive_on_exit {
            for tracked in self.processes.values_mut() {
                if tracked.state == ManagedProcessState::Running
                    && let Some(pid) = tracked.pid
                {
                    // Drop the Child handle without killing so the process group stays alive in background.
                    drop(tracked.child.take());
                    kept_pids.push(pid);
                }
            }
            return kept_pids;
        }

        let timeout = self.graceful_timeout;
        for tracked in self.processes.values_mut() {
            if let Some(mut child) = tracked.child.take() {
                let exit_code = stop_child_gracefully(&mut child, timeout);
                tracked.state = ManagedProcessState::Stopped;
                tracked.exited_at_ms = Some(now_ms());
                tracked.exit_code = exit_code;
                tracked.error_code = None;
                tracked.error_message = None;
            }
        }
        kept_pids
    }

    fn validate_scheme_files(&self, paths: &[PathBuf]) -> Vec<PaperSchemeEntry> {
        let Some(runtime) = self.runtime.as_ref() else {
            return paths
                .iter()
                .map(|p| {
                    fallback_parse_scheme_entry(
                        p,
                        Some("未找到 uv 或 talaria Python 运行时，无法校验交易方案 / Python runtime not found".to_string()),
                    )
                })
                .collect();
        };

        let Some(mut cmd) = runtime.build_validate_command(&self.data_dir, paths) else {
            // Custom test runtime: use lightweight YAML metadata parsing.
            return paths
                .iter()
                .map(|p| fallback_parse_scheme_entry(p, None))
                .collect();
        };

        cmd.stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let output = match cmd.output() {
            Ok(out) => out,
            Err(err) => {
                return paths
                    .iter()
                    .map(|p| {
                        fallback_parse_scheme_entry(
                            p,
                            Some(format!("执行方案校验失败 / Failed to execute validator: {err}")),
                        )
                    })
                    .collect();
            }
        };

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let err_msg = if stderr.is_empty() {
                format!("方案校验进程异常退出 ({})", output.status)
            } else {
                truncate_error(&stderr, 600)
            };
            return paths
                .iter()
                .map(|p| fallback_parse_scheme_entry(p, Some(err_msg.clone())))
                .collect();
        }

        #[derive(Deserialize)]
        struct RawValidationItem {
            path: String,
            valid: bool,
            scheme: String,
            #[serde(default)]
            sub_accounts: Vec<String>,
            error: Option<String>,
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        let parsed: Result<Vec<RawValidationItem>, _> = serde_json::from_str(stdout.trim());
        match parsed {
            Ok(items) => {
                let mut by_path: HashMap<String, RawValidationItem> =
                    items.into_iter().map(|it| (it.path.clone(), it)).collect();
                paths
                    .iter()
                    .map(|p| {
                        let key = p.to_string_lossy().into_owned();
                        let file_name = p
                            .file_name()
                            .map(|n| n.to_string_lossy().into_owned())
                            .unwrap_or_else(|| key.clone());
                        if let Some(item) = by_path.remove(&key) {
                            PaperSchemeEntry {
                                scheme: item.scheme,
                                path: key,
                                file_name,
                                sub_accounts: item.sub_accounts,
                                valid: item.valid,
                                validation_error: item.error,
                            }
                        } else {
                            fallback_parse_scheme_entry(
                                p,
                                Some("校验结果缺失 / Missing validation result".to_string()),
                            )
                        }
                    })
                    .collect()
            }
            Err(err) => paths
                .iter()
                .map(|p| {
                    fallback_parse_scheme_entry(
                        p,
                        Some(format!("无法解析校验输出 / Invalid validator JSON: {err}")),
                    )
                })
                .collect(),
        }
    }
}

pub fn resolve_schemes_dir(data_dir: &Path) -> PathBuf {
    if let Ok(explicit) = std::env::var("TALARIA_SCHEMES_DIR")
        && !explicit.trim().is_empty()
    {
        return PathBuf::from(explicit.trim());
    }
    data_dir.join(SCHEMES_DIR_NAME)
}

fn settings_path(data_dir: &Path) -> PathBuf {
    data_dir.join(SETTINGS_RELATIVE_PATH)
}

fn load_workbench_settings(data_dir: &Path) -> PersistedWorkbenchSettings {
    let path = settings_path(data_dir);
    let Ok(raw) = fs::read_to_string(&path) else {
        return PersistedWorkbenchSettings::default();
    };
    serde_json::from_str(&raw).unwrap_or_default()
}

fn save_workbench_settings(
    data_dir: &Path,
    settings: &PersistedWorkbenchSettings,
) -> Result<(), String> {
    let path = settings_path(data_dir);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create settings directory {}: {err}", parent.display()))?;
    }
    let tmp = path.with_extension("json.tmp");
    let body = serde_json::to_string_pretty(settings)
        .map_err(|err| format!("failed to serialize workbench settings: {err}"))?;
    fs::write(&tmp, format!("{body}\n"))
        .map_err(|err| format!("failed to write {}: {err}", tmp.display()))?;
    fs::rename(&tmp, &path)
        .map_err(|err| format!("failed to persist {}: {err}", path.display()))?;
    Ok(())
}

fn scan_scheme_files(schemes_dir: &Path) -> Result<Vec<PathBuf>, String> {
    if !schemes_dir.is_dir() {
        return Ok(Vec::new());
    }
    let entries = fs::read_dir(schemes_dir)
        .map_err(|err| format!("failed to read schemes dir {}: {err}", schemes_dir.display()))?;
    let mut files = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| e.to_ascii_lowercase());
        if matches!(ext.as_deref(), Some("yaml" | "yml")) {
            files.push(canonicalize_or_abs(&path));
        }
    }
    files.sort();
    Ok(files)
}

/// Lightweight fallback metadata extraction when Python runtime validation is skipped/unavailable.
fn fallback_parse_scheme_entry(path: &Path, runtime_error: Option<String>) -> PaperSchemeEntry {
    let file_name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_string_lossy().into_owned());
    let fallback_name = path
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "paper".to_string());

    let raw = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) => {
            return PaperSchemeEntry {
                scheme: fallback_name,
                path: path.to_string_lossy().into_owned(),
                file_name,
                sub_accounts: Vec::new(),
                valid: false,
                validation_error: Some(format!("无法读取方案文件: {err}")),
            };
        }
    };

    let mut scheme_name = fallback_name;
    let mut sub_accounts = Vec::new();
    let mut in_sub_accounts_block = false;
    let mut saw_scheme_key = false;

    for line in raw.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if !line.starts_with(' ') && !line.starts_with('\t') {
            in_sub_accounts_block = false;
            if let Some(rest) = trimmed.strip_prefix("scheme:") {
                let val = unquote_yaml_scalar(rest);
                if !val.is_empty() {
                    scheme_name = val;
                    saw_scheme_key = true;
                }
            } else if let Some(rest) = trimmed.strip_prefix("sub_accounts:") {
                let rest = rest.trim();
                if rest.starts_with('[') && rest.ends_with(']') {
                    let inner = &rest[1..rest.len() - 1];
                    for part in inner.split(',') {
                        let item = unquote_yaml_scalar(part);
                        if !item.is_empty() {
                            sub_accounts.push(item);
                        }
                    }
                } else if rest.is_empty() {
                    in_sub_accounts_block = true;
                }
            }
        } else if in_sub_accounts_block
            && let Some(item) = trimmed.strip_prefix('-')
        {
            let val = unquote_yaml_scalar(item);
            if !val.is_empty() {
                sub_accounts.push(val);
            }
        }
    }

    let validation_error = runtime_error.or_else(|| {
        if !saw_scheme_key {
            Some("交易方案缺少 scheme 字段 / Missing 'scheme' field".to_string())
        } else if sub_accounts.is_empty() {
            Some("交易方案缺少 sub_accounts 配置 / Missing 'sub_accounts'".to_string())
        } else {
            None
        }
    });

    PaperSchemeEntry {
        scheme: scheme_name,
        path: path.to_string_lossy().into_owned(),
        file_name,
        sub_accounts,
        valid: validation_error.is_none(),
        validation_error,
    }
}

fn unquote_yaml_scalar(raw: &str) -> String {
    let trimmed = raw.split('#').next().unwrap_or(raw).trim();
    if (trimmed.starts_with('"') && trimmed.ends_with('"') && trimmed.len() >= 2)
        || (trimmed.starts_with('\'') && trimmed.ends_with('\'') && trimmed.len() >= 2)
    {
        trimmed[1..trimmed.len() - 1].trim().to_string()
    } else {
        trimmed.to_string()
    }
}

pub fn scheme_slug(scheme: &str, scheme_path: &Path) -> String {
    let source = if scheme.trim().is_empty() {
        scheme_path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("paper")
    } else {
        scheme.trim()
    };
    let mut slug = String::with_capacity(source.len());
    let mut last_dash = false;
    for ch in source.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' {
            slug.push(ch.to_ascii_lowercase());
            last_dash = false;
        } else if !last_dash && !slug.is_empty() {
            slug.push('-');
            last_dash = true;
        }
    }
    let slug = slug.trim_matches('-').to_string();
    if slug.is_empty() {
        "paper".to_string()
    } else {
        slug
    }
}

pub fn log_path_for_scheme(data_dir: &Path, scheme: &str, scheme_path: &Path) -> PathBuf {
    data_dir
        .join(LOGS_DIR_NAME)
        .join(format!("account-{}.log", scheme_slug(scheme, scheme_path)))
}

fn open_log_files(
    log_path: &Path,
    scheme: &str,
    scheme_path: &Path,
) -> Result<(File, File, u64), String> {
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| format!("failed to create logs directory {}: {err}", parent.display()))?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .map_err(|err| format!("failed to open log file {}: {err}", log_path.display()))?;

    let start_offset = file.seek(SeekFrom::End(0)).unwrap_or(0);
    let _ = writeln!(
        file,
        "=== [{}] Starting paper account '{}' ({}) ===",
        now_ms(),
        scheme,
        scheme_path.display()
    );
    let _ = file.flush();
    let after_header_offset = file.seek(SeekFrom::End(0)).unwrap_or(start_offset);

    let stderr_file = file
        .try_clone()
        .map_err(|err| format!("failed to clone log handle: {err}"))?;
    Ok((file, stderr_file, after_header_offset))
}

fn read_log_tail_since(log_path: &Path, offset: u64) -> String {
    let Ok(mut file) = File::open(log_path) else {
        return String::new();
    };
    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
    let read_from = offset.min(len);
    let max_bytes: u64 = 8192;
    let start = if len.saturating_sub(read_from) > max_bytes {
        len - max_bytes
    } else {
        read_from
    };
    if file.seek(SeekFrom::Start(start)).is_err() {
        return String::new();
    }
    let mut buf = Vec::new();
    let _ = file.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).trim().to_string()
}

pub fn is_lock_conflict_text(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    text.contains("已被另一账户进程占用")
        || text.contains("已被其他进程占用")
        || text.contains("AccountOwnershipError")
        || lower.contains("owner.lock")
        || lower.contains("already held by another process")
}

fn format_lock_conflict_message(detail: &str) -> String {
    let clean = detail.trim();
    if clean.is_empty() {
        "该子账户已被其他进程占用 / Sub-account is already held by another process".to_string()
    } else {
        format!(
            "该子账户已被其他进程占用 / Sub-account is already held by another process: {}",
            truncate_error(clean, 360)
        )
    }
}

fn classify_exit_error(log_tail: &str, exit_code: Option<i32>) -> (String, String) {
    if is_lock_conflict_text(log_tail) {
        let relevant = log_tail
            .lines()
            .rev()
            .find(|line| !line.trim().is_empty())
            .unwrap_or(log_tail);
        return (
            ERROR_LOCK_CONFLICT.to_string(),
            format_lock_conflict_message(relevant),
        );
    }
    let detail = if log_tail.is_empty() {
        match exit_code {
            Some(code) => format!("账户进程异常退出 (exit code {code})"),
            None => "账户进程因信号异常终止 / Account process terminated unexpectedly".to_string(),
        }
    } else {
        truncate_error(log_tail, 600)
    };
    (ERROR_CRASHED.to_string(), detail)
}

fn truncate_error(text: &str, max_chars: usize) -> String {
    let count = text.chars().count();
    if count <= max_chars {
        return text.to_string();
    }
    let prefix: String = text.chars().take(max_chars).collect();
    format!("{prefix}…")
}

fn stop_child_gracefully(child: &mut Child, timeout: Duration) -> Option<i32> {
    if let Ok(Some(status)) = child.try_wait() {
        return status.code();
    }

    #[cfg(unix)]
    {
        let pid = child.id();
        // Send SIGINT first so Python's `run_paper` catches KeyboardInterrupt,
        // transitions sub-accounts to REDUCING (`PAPER_STOPPING`), and writes stopped projection.
        send_signal_to_group(pid, libc::SIGINT);

        let sigint_budget = timeout
            .mul_f32(0.7)
            .max(Duration::from_millis(200))
            .min(timeout);
        let sigint_deadline = Instant::now() + sigint_budget;
        while Instant::now() < sigint_deadline {
            match child.try_wait() {
                Ok(Some(status)) => return status.code(),
                Ok(None) => thread::sleep(Duration::from_millis(40)),
                Err(_) => break,
            }
        }

        // Escalate to SIGTERM for remaining grace period.
        send_signal_to_group(pid, libc::SIGTERM);
        let sigterm_deadline = Instant::now() + timeout.saturating_sub(sigint_budget);
        while Instant::now() < sigterm_deadline {
            match child.try_wait() {
                Ok(Some(status)) => return status.code(),
                Ok(None) => thread::sleep(Duration::from_millis(40)),
                Err(_) => break,
            }
        }

        // Timeout exceeded: force kill process group and reap child.
        send_signal_to_group(pid, libc::SIGKILL);
        let _ = child.kill();
        return child.wait().ok().and_then(|s| s.code());
    }

    #[cfg(not(unix))]
    {
        let _ = timeout;
        let _ = child.kill();
        child.wait().ok().and_then(|s| s.code())
    }
}

#[cfg(unix)]
fn send_signal_to_group(pid: u32, signal: libc::c_int) {
    let pgid = -(pid as libc::pid_t);
    // SAFETY: `pid` is a child process spawned with `process_group(0)`.
    let rc = unsafe { libc::kill(pgid, signal) };
    if rc != 0 {
        unsafe {
            libc::kill(pid as libc::pid_t, signal);
        }
    }
}

fn open_path_in_os(path: &Path) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = Command::new("open");
        c.arg(path);
        c
    };

    #[cfg(target_os = "linux")]
    let mut cmd = {
        let mut c = Command::new("xdg-open");
        c.arg(path);
        c
    };

    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut c = Command::new("cmd");
        c.args(["/C", "start", ""]).arg(path);
        c
    };

    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| format!("failed to open {}: {err}", path.display()))?;
    Ok(())
}

fn canonicalize_or_abs(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| {
        if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()
                .map(|cwd| cwd.join(path))
                .unwrap_or_else(|_| path.to_path_buf())
        }
    })
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn extra_bin_dirs() -> Vec<PathBuf> {
    let mut dirs = vec![
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ];
    if let Ok(home) = std::env::var("HOME") {
        let home_path = PathBuf::from(home);
        dirs.push(home_path.join(".local").join("bin"));
        dirs.push(home_path.join(".cargo").join("bin"));
    }
    dirs
}

fn augment_path_env(cmd: &mut Command) {
    let mut paths: Vec<PathBuf> = std::env::var_os("PATH")
        .map(|raw| std::env::split_paths(&raw).collect())
        .unwrap_or_default();
    for dir in extra_bin_dirs() {
        if !paths.contains(&dir) {
            paths.push(dir);
        }
    }
    if let Ok(joined) = std::env::join_paths(paths) {
        cmd.env("PATH", joined);
    }
}

fn find_executable(name: &str) -> Option<PathBuf> {
    let file_name = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    };

    if let Some(raw_path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&raw_path) {
            let candidate = dir.join(&file_name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }

    for dir in extra_bin_dirs() {
        let candidate = dir.join(&file_name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }

    None
}

fn find_talaria_binary() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("TALARIA_BIN")
        && !explicit.trim().is_empty()
    {
        let p = PathBuf::from(explicit.trim());
        if p.is_file() {
            return Some(p);
        }
    }
    if let Ok(exe) = std::env::current_exe()
        && let Some(dir) = exe.parent()
    {
        let sibling = dir.join(if cfg!(windows) { "talaria.exe" } else { "talaria" });
        if sibling.is_file() {
            return Some(sibling);
        }
    }
    find_executable("talaria")
}

fn is_talaria_project_root(dir: &Path) -> bool {
    dir.join("pyproject.toml").is_file()
        && (dir.join("core").join("src").join("talaria").is_dir()
            || dir.join("src").join("talaria").is_dir())
}

fn find_project_dir() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("TALARIA_PROJECT_DIR")
        && !explicit.trim().is_empty()
    {
        let p = PathBuf::from(explicit.trim());
        if is_talaria_project_root(&p) {
            return Some(p);
        }
    }

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    for ancestor in manifest_dir.ancestors() {
        if is_talaria_project_root(ancestor) {
            return Some(ancestor.to_path_buf());
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors() {
            if is_talaria_project_root(ancestor) {
                return Some(ancestor.to_path_buf());
            }
        }
    }

    if let Ok(cwd) = std::env::current_dir() {
        for ancestor in cwd.ancestors() {
            if is_talaria_project_root(ancestor) {
                return Some(ancestor.to_path_buf());
            }
        }
    }

    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQ: AtomicU64 = AtomicU64::new(1);

    fn temp_test_dir(label: &str) -> PathBuf {
        let seq = TEST_SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "talaria-tauri-account-{label}-{}-{seq}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join(SCHEMES_DIR_NAME)).unwrap();
        dir
    }

    fn write_sample_scheme(dir: &Path, file_name: &str, scheme_name: &str, sub_account: &str) -> PathBuf {
        let path = dir.join(SCHEMES_DIR_NAME).join(file_name);
        fs::write(
            &path,
            format!(
                "scheme: {scheme_name}\ninterface_version: 2\nsub_accounts:\n  - {sub_account}\n"
            ),
        )
        .unwrap();
        path
    }

    fn python3_bin() -> PathBuf {
        find_executable("python3").expect("python3 must be available for unit tests")
    }

    /// Writes a lightweight Python mock account process that:
    /// - optionally acquires flock on `<data_dir>/accounts/<sub_account>/owner.lock`
    /// - handles SIGINT by writing a marker file (`graceful-stop.txt`) and exiting 0
    /// - optionally ignores SIGINT/SIGTERM (for force-kill timeout tests)
    /// - optionally exits non-zero after a short delay (for crash detection tests)
    fn write_mock_account_script(dir: &Path, name: &str, body: &str) -> PathBuf {
        let script = dir.join(name);
        fs::write(&script, body).unwrap();
        script
    }

    #[cfg(unix)]
    fn pid_is_alive(pid: u32) -> bool {
        // SAFETY: signal 0 only probes process existence.
        unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
    }

    #[cfg(unix)]
    fn cleanup_pid(pid: u32) {
        unsafe {
            libc::kill(-(pid as libc::pid_t), libc::SIGKILL);
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
            let mut status: libc::c_int = 0;
            libc::waitpid(pid as libc::pid_t, &mut status, 0);
        }
    }

    // Source: Ticket 09 — Start, single-instance guard, log capture, and graceful SIGINT stop.
    #[test]
    fn start_enforces_single_instance_and_graceful_stop_runs_sigint_hook() {
        let dir = temp_test_dir("start-stop");
        let scheme = write_sample_scheme(&dir, "btc.yaml", "btc-paper", "sa-main");
        let marker = dir.join("graceful-stop.txt");

        let script = write_mock_account_script(
            &dir,
            "mock_account.py",
            &format!(
                r#"
import signal, sys, time
from pathlib import Path

marker = Path({marker:?})

def handle_sigint(signum, frame):
    marker.write_text("PAPER_STOPPING:REDUCING", encoding="utf-8")
    print("graceful shutdown complete", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, handle_sigint)
print("account process started", flush=True)
while True:
    time.sleep(0.05)
"#,
                marker = marker.to_string_lossy()
            ),
        );

        let runtime = PythonRuntime::CustomCommand {
            program: python3_bin(),
            args_prefix: vec![script.to_string_lossy().into_owned()],
            cwd: None,
        };
        let mut mgr = AccountProcessManager::with_runtime(
            dir.clone(),
            dir.join(SCHEMES_DIR_NAME),
            Some(runtime),
        );
        mgr.set_timeouts(Duration::from_secs(3), Duration::from_millis(200));

        let started = mgr.start_scheme(&scheme).expect("first launch should succeed");
        assert_eq!(started.state, ManagedProcessState::Running);
        assert_eq!(started.scheme, "btc-paper");
        assert_eq!(started.sub_accounts, vec!["sa-main".to_string()]);
        assert!(started.pid.is_some());
        assert!(Path::new(&started.log_path).is_file());

        // Single-instance guard rejects duplicate launch of the same scheme.
        let dup_err = mgr
            .start_scheme(&scheme)
            .expect_err("duplicate launch must be rejected");
        assert!(dup_err.contains(ERROR_ALREADY_RUNNING), "got: {dup_err}");

        // Graceful stop sends SIGINT -> child runs hook -> exits cleanly.
        let stopped = mgr.stop_scheme(&scheme).expect("stop should succeed");
        assert_eq!(stopped.state, ManagedProcessState::Stopped);
        assert_eq!(stopped.exit_code, Some(0));
        assert_eq!(
            fs::read_to_string(&marker).unwrap(),
            "PAPER_STOPPING:REDUCING"
        );

        let log_body = fs::read_to_string(&started.log_path).unwrap();
        assert!(log_body.contains("account process started"));
        assert!(log_body.contains("graceful shutdown complete"));

        let _ = fs::remove_dir_all(&dir);
    }

    // Source: Ticket 09 — Timeout escalates to SIGKILL when process ignores SIGINT/SIGTERM.
    #[test]
    fn graceful_stop_escalates_to_sigkill_after_timeout() {
        let dir = temp_test_dir("sigkill-fallback");
        let scheme = write_sample_scheme(&dir, "stubborn.yaml", "stubborn-paper", "sa-stubborn");

        let script = write_mock_account_script(
            &dir,
            "stubborn.py",
            r#"
import signal, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ignoring signals", flush=True)
while True:
    time.sleep(0.05)
"#,
        );

        let runtime = PythonRuntime::CustomCommand {
            program: python3_bin(),
            args_prefix: vec![script.to_string_lossy().into_owned()],
            cwd: None,
        };
        let mut mgr = AccountProcessManager::with_runtime(
            dir.clone(),
            dir.join(SCHEMES_DIR_NAME),
            Some(runtime),
        );
        mgr.set_timeouts(Duration::from_millis(450), Duration::from_millis(150));

        let started = mgr.start_scheme(&scheme).unwrap();
        let pid = started.pid.unwrap();
        let stopped = mgr.stop_scheme(&scheme).unwrap();
        assert_eq!(stopped.state, ManagedProcessState::Stopped);

        #[cfg(unix)]
        {
            thread::sleep(Duration::from_millis(50));
            assert!(!pid_is_alive(pid), "stubborn process should be killed");
        }

        let _ = fs::remove_dir_all(&dir);
    }

    // Source: Ticket 09 — Crash detection captures stderr & allows clean restart.
    #[test]
    fn detects_crash_and_supports_clean_restart() {
        let dir = temp_test_dir("crash-restart");
        let scheme = write_sample_scheme(&dir, "crashy.yaml", "crashy-paper", "sa-crash");
        let flag = dir.join("should-stay-alive.txt");

        let script = write_mock_account_script(
            &dir,
            "crashy.py",
            &format!(
                r#"
import signal, sys, time
from pathlib import Path

flag = Path({flag:?})
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
if not flag.exists():
    time.sleep(0.25)
    print("fatal: simulated exchange websocket crash", file=sys.stderr, flush=True)
    sys.exit(9)
print("restarted healthy", flush=True)
while True:
    time.sleep(0.05)
"#,
                flag = flag.to_string_lossy()
            ),
        );

        let runtime = PythonRuntime::CustomCommand {
            program: python3_bin(),
            args_prefix: vec![script.to_string_lossy().into_owned()],
            cwd: None,
        };
        let mut mgr = AccountProcessManager::with_runtime(
            dir.clone(),
            dir.join(SCHEMES_DIR_NAME),
            Some(runtime),
        );
        mgr.set_timeouts(Duration::from_secs(2), Duration::from_millis(100));

        let first = mgr.start_scheme(&scheme).unwrap();
        assert_eq!(first.state, ManagedProcessState::Running);

        // Wait until process crashes after ~250ms.
        let mut crashed_status = None;
        for _ in 0..30 {
            let list = mgr.list_processes();
            if list[0].state == ManagedProcessState::Crashed {
                crashed_status = Some(list[0].clone());
                break;
            }
            thread::sleep(Duration::from_millis(40));
        }
        let crashed = crashed_status.expect("manager must detect crashed child");
        assert_eq!(crashed.exit_code, Some(9));
        assert_eq!(crashed.error_code.as_deref(), Some(ERROR_CRASHED));
        assert!(
            crashed
                .error_message
                .as_deref()
                .unwrap_or("")
                .contains("simulated exchange websocket crash")
        );

        // Restart cleanly after crash.
        fs::write(&flag, "ok").unwrap();
        let restarted = mgr.start_scheme(&scheme).expect("restart after crash should succeed");
        assert_eq!(restarted.state, ManagedProcessState::Running);
        assert_ne!(restarted.pid, first.pid);

        let _ = mgr.stop_scheme(&scheme);
        let _ = fs::remove_dir_all(&dir);
    }

    // Source: Ticket 09 — Lock conflict on `accounts/<sub_account>/owner.lock` surfaces clear message.
    #[test]
    fn detects_owner_lock_conflict_and_surfaces_clear_message() {
        let dir = temp_test_dir("lock-conflict");
        let scheme = write_sample_scheme(&dir, "locked.yaml", "locked-paper", "sa-locked");

        // Hold an external flock on accounts/sa-locked/owner.lock via a separate Python helper process.
        let holder_ready = dir.join("holder.ready");
        let holder_script = write_mock_account_script(
            &dir,
            "hold_lock.py",
            &format!(
                r#"
import fcntl, os, sys, time
from pathlib import Path

lock_path = Path({lock:?})
lock_path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
Path({ready:?}).write_text("locked", encoding="utf-8")
while True:
    time.sleep(0.1)
"#,
                lock = dir
                    .join("accounts")
                    .join("sa-locked")
                    .join("owner.lock")
                    .to_string_lossy(),
                ready = holder_ready.to_string_lossy()
            ),
        );

        let mut holder = Command::new(python3_bin())
            .arg(&holder_script)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();

        for _ in 0..40 {
            if holder_ready.is_file() {
                break;
            }
            thread::sleep(Duration::from_millis(25));
        }
        assert!(holder_ready.is_file(), "lock holder did not acquire lock");

        // Mock account process mirrors `talaria.account.ownership.hold_sub_accounts`.
        let account_script = write_mock_account_script(
            &dir,
            "try_lock_account.py",
            &format!(
                r#"
import fcntl, os, sys
from pathlib import Path

lock_path = Path({lock:?})
lock_path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print(f"账户进程失败：子账户 sa-locked 已被另一账户进程占用（{{lock_path}}）", file=sys.stderr, flush=True)
    sys.exit(2)
"#,
                lock = dir
                    .join("accounts")
                    .join("sa-locked")
                    .join("owner.lock")
                    .to_string_lossy()
            ),
        );

        let runtime = PythonRuntime::CustomCommand {
            program: python3_bin(),
            args_prefix: vec![account_script.to_string_lossy().into_owned()],
            cwd: None,
        };
        let mut mgr = AccountProcessManager::with_runtime(
            dir.clone(),
            dir.join(SCHEMES_DIR_NAME),
            Some(runtime),
        );
        mgr.set_timeouts(Duration::from_secs(2), Duration::from_millis(600));

        let err = mgr
            .start_scheme(&scheme)
            .expect_err("launch must fail when owner.lock is held");
        assert!(err.contains(ERROR_LOCK_CONFLICT), "got: {err}");
        assert!(
            err.contains("该子账户已被其他进程占用"),
            "expected clear Chinese lock conflict message, got: {err}"
        );
        assert!(
            err.contains("Sub-account is already held by another process"),
            "expected clear English lock conflict message, got: {err}"
        );

        let _ = holder.kill();
        let _ = holder.wait();
        let _ = fs::remove_dir_all(&dir);
    }

    // Source: Ticket 09 & spec D9 — Default exit stops spawned processes; keep-alive toggle persists and preserves them.
    #[test]
    fn workbench_exit_stops_by_default_and_preserves_when_keep_alive_enabled() {
        let dir = temp_test_dir("exit-policy");
        let scheme = write_sample_scheme(&dir, "policy.yaml", "policy-paper", "sa-policy");
        let script = write_mock_account_script(
            &dir,
            "sleeper.py",
            r#"
import signal, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
while True:
    time.sleep(0.05)
"#,
        );

        let make_runtime = || PythonRuntime::CustomCommand {
            program: python3_bin(),
            args_prefix: vec![script.to_string_lossy().into_owned()],
            cwd: None,
        };

        // Case 1: Default (keep_alive_on_exit == false) stops child on workbench exit.
        let mut mgr_default = AccountProcessManager::with_runtime(
            dir.clone(),
            dir.join(SCHEMES_DIR_NAME),
            Some(make_runtime()),
        );
        assert!(!mgr_default.keep_alive_on_exit());
        mgr_default.set_timeouts(Duration::from_secs(2), Duration::from_millis(120));

        let started1 = mgr_default.start_scheme(&scheme).unwrap();
        let pid1 = started1.pid.unwrap();
        let kept1 = mgr_default.on_workbench_exit();
        assert!(kept1.is_empty());
        #[cfg(unix)]
        {
            thread::sleep(Duration::from_millis(50));
            assert!(!pid_is_alive(pid1), "default exit must stop spawned account process");
        }

        // Case 2: Enabling keep_alive persists to disk and keeps child alive on exit.
        assert_eq!(mgr_default.set_keep_alive_on_exit(true), Ok(true));

        let mut mgr_reloaded = AccountProcessManager::with_runtime(
            dir.clone(),
            dir.join(SCHEMES_DIR_NAME),
            Some(make_runtime()),
        );
        assert!(
            mgr_reloaded.keep_alive_on_exit(),
            "keep-alive setting must persist across restarts"
        );
        mgr_reloaded.set_timeouts(Duration::from_secs(2), Duration::from_millis(120));

        let started2 = mgr_reloaded.start_scheme(&scheme).unwrap();
        let pid2 = started2.pid.unwrap();
        let kept2 = mgr_reloaded.on_workbench_exit();
        assert_eq!(kept2, vec![pid2]);

        #[cfg(unix)]
        {
            thread::sleep(Duration::from_millis(80));
            assert!(
                pid_is_alive(pid2),
                "keep-alive exit must leave spawned account process running in background"
            );
            cleanup_pid(pid2);
        }

        let _ = fs::remove_dir_all(&dir);
    }

    // Source: Ticket 09 — Scheme discovery & real Python pre-launch validation catches unapproved agent strategies.
    #[test]
    fn discovers_schemes_and_flags_unapproved_agent_strategy() {
        let Some(runtime) = PythonRuntime::discover() else {
            // Skip only if no Python/uv runtime is installed on host.
            return;
        };

        let dir = temp_test_dir("validate-agent");
        let schemes_dir = dir.join(SCHEMES_DIR_NAME);
        let strategy_py = schemes_dir.join("unregistered_agent.py");
        fs::write(&strategy_py, "plugin = None\n").unwrap();

        let bad_scheme = schemes_dir.join("agent-unapproved.yaml");
        fs::write(
            &bad_scheme,
            r#"
scheme: agent-unapproved
interface_version: 2
sub_accounts:
  - sa-agent
slots:
  data:
    plugin: live_ws
    params:
      instrument: BTC-USDT-PERP
      timeframe: 1m
  strategy:
    python: unregistered_agent.py
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
  tools: []
"#,
        )
        .unwrap();

        let mgr = AccountProcessManager::with_runtime(dir.clone(), schemes_dir, Some(runtime));
        let listed = mgr.list_schemes().expect("list_schemes should succeed");
        assert_eq!(listed.schemes.len(), 1);
        let item = &listed.schemes[0];
        assert_eq!(item.scheme, "agent-unapproved");
        assert_eq!(item.sub_accounts, vec!["sa-agent".to_string()]);
        assert!(!item.valid, "unregistered/unapproved python strategy must fail validation");
        let err = item.validation_error.as_deref().unwrap_or("");
        assert!(
            err.contains("策略代码未登记") || err.contains("agent 策略未批准"),
            "expected clear strategy approval/registration error, got: {err}"
        );

        let _ = fs::remove_dir_all(&dir);
    }
}
