import { useCallback, useEffect, useMemo, useState } from "react";
import type { PaperAccountView } from "@talaria/contracts";

import { getLocale, t, useLocale } from "../../lib/i18n";
import {
  getTauriPaperKeepAlive,
  isTauriDesktop,
  listTauriPaperProcesses,
  listTauriPaperSchemes,
  openTauriPaperLog,
  setTauriPaperKeepAlive,
  startTauriPaperAccount,
  stopTauriPaperAccount,
  type PaperProcessStatus,
  type PaperSchemeEntry,
} from "../../lib/tauriBridge";
import { isPaperProcessOnline } from "./usePaperAccounts";
import "./PaperProcessControl.css";

export type ResolvedProcessPhase =
  | "ONLINE"
  | "STARTING"
  | "CRASHED"
  | "FAILED"
  | "STOPPED"
  | "OFFLINE";

export interface ResolvedPaperControlStatus {
  phase: ResolvedProcessPhase;
  origin: "MANAGED" | "EXTERNAL" | "NONE";
  pid: number | null;
  schemeName: string | null;
  heartbeatTs: number | null;
  exitCode: number | null;
  errorCode: string | null;
  errorMessage: string | null;
}

export interface PaperProcessControlProps {
  views?: readonly PaperAccountView[];
  selectedView?: PaperAccountView | null;
  onRefreshAccounts?: () => void | Promise<void>;
  desktopMode?: boolean;
  dataDir?: string;
  pollIntervalMs?: number;
}

function formatError(err: unknown): string {
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message;
  return String(err);
}

function formatTime(ts: number): string {
  return new Intl.DateTimeFormat(getLocale() === "zh" ? "zh-CN" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(ts);
}

/**
 * Resolve paper account process status grounded in projection heartbeat (`PaperAccountView.process`)
 * and supplemented by the Tauri process handle (for starting, crash details, and external CLI runs).
 */
export function resolvePaperControlStatus(
  views: readonly PaperAccountView[],
  selectedView: PaperAccountView | null | undefined,
  selectedScheme: PaperSchemeEntry | null,
  managed: PaperProcessStatus | null,
): ResolvedPaperControlStatus {
  const matchingViews: PaperAccountView[] = selectedScheme
    ? views.filter(
        (v) =>
          selectedScheme.sub_accounts.includes(v.sub_account_id) ||
          v.process?.scheme === selectedScheme.scheme,
      )
    : selectedView
      ? [selectedView]
      : [...views];

  const runningView = matchingViews.find((v) => isPaperProcessOnline(v)) ?? null;
  if (runningView?.process) {
    const isManagedRunning = managed?.state === "RUNNING";
    return {
      phase: "ONLINE",
      origin: isManagedRunning ? "MANAGED" : "EXTERNAL",
      pid: runningView.process.pid,
      schemeName: runningView.process.scheme,
      heartbeatTs: runningView.process.heartbeat_ts,
      exitCode: null,
      errorCode: null,
      errorMessage: null,
    };
  }

  if (managed?.state === "RUNNING") {
    return {
      phase: "STARTING",
      origin: "MANAGED",
      pid: managed.pid,
      schemeName: managed.scheme,
      heartbeatTs: null,
      exitCode: null,
      errorCode: null,
      errorMessage: null,
    };
  }

  if (managed?.state === "CRASHED") {
    return {
      phase: "CRASHED",
      origin: "MANAGED",
      pid: managed.pid,
      schemeName: managed.scheme,
      heartbeatTs: null,
      exitCode: managed.exit_code,
      errorCode: managed.error_code,
      errorMessage: managed.error_message,
    };
  }

  if (managed?.state === "FAILED") {
    return {
      phase: "FAILED",
      origin: "MANAGED",
      pid: managed.pid,
      schemeName: managed.scheme,
      heartbeatTs: null,
      exitCode: managed.exit_code,
      errorCode: managed.error_code,
      errorMessage: managed.error_message,
    };
  }

  const stoppedView = matchingViews.find((v) => v.process?.stopped_ts != null) ?? null;
  if (managed?.state === "STOPPED" || stoppedView?.process) {
    return {
      phase: "STOPPED",
      origin: managed ? "MANAGED" : "NONE",
      pid: managed?.pid ?? stoppedView?.process?.pid ?? null,
      schemeName: managed?.scheme ?? stoppedView?.process?.scheme ?? selectedScheme?.scheme ?? null,
      heartbeatTs: stoppedView?.process?.heartbeat_ts ?? null,
      exitCode: managed?.exit_code ?? null,
      errorCode: null,
      errorMessage: null,
    };
  }

  const offlineView = matchingViews.find((v) => v.process != null) ?? null;
  return {
    phase: "OFFLINE",
    origin: "NONE",
    pid: offlineView?.process?.pid ?? null,
    schemeName: offlineView?.process?.scheme ?? selectedScheme?.scheme ?? null,
    heartbeatTs: offlineView?.process?.heartbeat_ts ?? null,
    exitCode: null,
    errorCode: null,
    errorMessage: null,
  };
}

export function buildPaperCliCommand(options: {
  scheme?: string | null;
  dataDir?: string | null;
}): string {
  const dir = options.dataDir?.trim() || "~/.talaria/data";
  const schemeArg = options.scheme?.trim()
    ? `${dir}/schemes/${options.scheme.trim()}.yaml`
    : "<scheme.yaml>";
  return `uv run talaria account run ${schemeArg} --mode paper --data-dir ${dir}`;
}

function statusBadgeLabel(resolved: ResolvedPaperControlStatus): string {
  switch (resolved.phase) {
    case "ONLINE":
      return resolved.origin === "EXTERNAL"
        ? t("在线 · 外部进程", "Online · External")
        : t("运行中", "Running");
    case "STARTING":
      return t("启动中", "Starting");
    case "CRASHED":
      return t("已崩溃", "Crashed");
    case "FAILED":
      return t("启动失败", "Launch failed");
    case "STOPPED":
      return t("已停止", "Stopped");
    case "OFFLINE":
      return t("未运行", "Offline");
  }
}

function statusBadgeClass(phase: ResolvedProcessPhase): string {
  switch (phase) {
    case "ONLINE":
      return "paper-proc-badge paper-proc-badge--online";
    case "STARTING":
      return "paper-proc-badge paper-proc-badge--starting";
    case "CRASHED":
    case "FAILED":
      return "paper-proc-badge paper-proc-badge--danger";
    case "STOPPED":
    case "OFFLINE":
      return "paper-proc-badge paper-proc-badge--offline";
  }
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fallback below.
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

export function PaperProcessControl({
  views = [],
  selectedView = null,
  onRefreshAccounts,
  desktopMode,
  dataDir,
  pollIntervalMs = 2000,
}: PaperProcessControlProps) {
  useLocale();
  const isDesktop = desktopMode ?? isTauriDesktop();

  const [schemesDir, setSchemesDir] = useState<string>("");
  const [resolvedDataDir, setResolvedDataDir] = useState<string>(dataDir ?? "~/.talaria/data");
  const [schemes, setSchemes] = useState<PaperSchemeEntry[]>([]);
  const [selectedSchemePath, setSelectedSchemePath] = useState<string>("");
  const [processes, setProcesses] = useState<PaperProcessStatus[]>([]);
  const [keepAlive, setKeepAlive] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(isDesktop);
  const [busyAction, setBusyAction] = useState<"start" | "stop" | "restart" | "log" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [copied, setCopied] = useState<boolean>(false);

  const loadDesktopState = useCallback(async (withSpinner = false) => {
    if (!isDesktop) return;
    if (withSpinner) setLoading(true);
    try {
      const [schemeList, procList, keepAliveSetting] = await Promise.all([
        listTauriPaperSchemes(),
        listTauriPaperProcesses(),
        getTauriPaperKeepAlive(),
      ]);
      if (schemeList) {
        setSchemesDir(schemeList.schemes_dir);
        setResolvedDataDir(schemeList.data_dir);
        setSchemes(schemeList.schemes);
        setSelectedSchemePath((prev) => {
          if (prev && schemeList.schemes.some((s) => s.path === prev)) {
            return prev;
          }
          return schemeList.schemes[0]?.path ?? "";
        });
      }
      if (procList) {
        setProcesses(procList);
      }
      if (typeof keepAliveSetting === "boolean") {
        setKeepAlive(keepAliveSetting);
      }
    } catch (err) {
      setActionError(formatError(err));
    } finally {
      if (withSpinner) setLoading(false);
    }
  }, [isDesktop]);

  useEffect(() => {
    if (!isDesktop) return;
    void loadDesktopState(true);
  }, [isDesktop, loadDesktopState]);

  useEffect(() => {
    if (!isDesktop || pollIntervalMs <= 0) return;
    const timer = window.setInterval(() => {
      void listTauriPaperProcesses()
        .then((list) => {
          if (list) setProcesses(list);
        })
        .catch(() => undefined);
    }, pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [isDesktop, pollIntervalMs]);

  const selectedScheme = useMemo(
    () => schemes.find((item) => item.path === selectedSchemePath) ?? null,
    [schemes, selectedSchemePath],
  );

  const managedProcess = useMemo(() => {
    if (!selectedScheme) return null;
    return (
      processes.find((p) => p.scheme_path === selectedScheme.path) ??
      processes.find((p) => p.scheme === selectedScheme.scheme) ??
      null
    );
  }, [processes, selectedScheme]);

  const resolved = useMemo(
    () => resolvePaperControlStatus(views, selectedView, selectedScheme, managedProcess),
    [views, selectedView, selectedScheme, managedProcess],
  );

  const handleStart = async () => {
    if (!selectedScheme) return;
    setBusyAction("start");
    setActionError(null);
    try {
      await startTauriPaperAccount(selectedScheme.path);
      const nextProcs = await listTauriPaperProcesses();
      if (nextProcs) setProcesses(nextProcs);
      await onRefreshAccounts?.();
    } catch (err) {
      setActionError(formatError(err));
      const nextProcs = await listTauriPaperProcesses().catch(() => null);
      if (nextProcs) setProcesses(nextProcs);
    } finally {
      setBusyAction(null);
    }
  };

  const handleStop = async () => {
    const targetPath = managedProcess?.scheme_path ?? selectedScheme?.path;
    if (!targetPath) return;
    setBusyAction("stop");
    setActionError(null);
    try {
      await stopTauriPaperAccount(targetPath);
      const nextProcs = await listTauriPaperProcesses();
      if (nextProcs) setProcesses(nextProcs);
      await onRefreshAccounts?.();
    } catch (err) {
      setActionError(formatError(err));
    } finally {
      setBusyAction(null);
    }
  };

  const handleRestart = async () => {
    if (!selectedScheme) return;
    setBusyAction("restart");
    setActionError(null);
    try {
      if (managedProcess?.state === "RUNNING") {
        await stopTauriPaperAccount(managedProcess.scheme_path);
      }
      await startTauriPaperAccount(selectedScheme.path);
      const nextProcs = await listTauriPaperProcesses();
      if (nextProcs) setProcesses(nextProcs);
      await onRefreshAccounts?.();
    } catch (err) {
      setActionError(formatError(err));
      const nextProcs = await listTauriPaperProcesses().catch(() => null);
      if (nextProcs) setProcesses(nextProcs);
    } finally {
      setBusyAction(null);
    }
  };

  const handleOpenLog = async () => {
    const target = managedProcess?.scheme_path ?? selectedScheme?.path;
    if (!target) return;
    setBusyAction("log");
    setActionError(null);
    try {
      await openTauriPaperLog(target);
    } catch (err) {
      setActionError(formatError(err));
    } finally {
      setBusyAction(null);
    }
  };

  const handleToggleKeepAlive = async (next: boolean) => {
    setKeepAlive(next);
    setActionError(null);
    try {
      const persisted = await setTauriPaperKeepAlive(next);
      if (typeof persisted === "boolean") {
        setKeepAlive(persisted);
      }
    } catch (err) {
      setKeepAlive(!next);
      setActionError(formatError(err));
    }
  };

  const cliCommand = useMemo(
    () =>
      buildPaperCliCommand({
        scheme: selectedView?.process?.scheme ?? resolved.schemeName,
        dataDir: dataDir ?? resolvedDataDir,
      }),
    [dataDir, resolvedDataDir, resolved.schemeName, selectedView?.process?.scheme],
  );

  const handleCopyCli = async () => {
    const ok = await copyText(cliCommand);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    }
  };

  const displayError =
    actionError ??
    (resolved.phase === "CRASHED" || resolved.phase === "FAILED" ? resolved.errorMessage : null);

  if (!isDesktop) {
    return (
      <section
        className="paper-proc-card paper-proc-card--browser"
        data-testid="paper-process-control"
        data-mode="browser"
        aria-label={t("模拟盘进程状态", "Paper Account Process Status")}
      >
        <div className="paper-proc-head">
          <div className="paper-proc-title-wrap">
            <h3>{t("模拟盘进程", "Paper Process")}</h3>
            <span className={statusBadgeClass(resolved.phase)} data-testid="paper-proc-status">
              {statusBadgeLabel(resolved)}
            </span>
          </div>
          <span className="paper-proc-mode-chip" data-testid="paper-proc-mode-chip">
            {t("浏览器只读", "Browser read-only")}
          </span>
        </div>

        {(resolved.schemeName || resolved.pid != null || resolved.heartbeatTs != null) && (
          <div className="paper-proc-meta" data-testid="paper-proc-meta">
            {resolved.schemeName && (
              <span>
                {t("方案", "Scheme")}: {resolved.schemeName}
              </span>
            )}
            {resolved.pid != null && <span>PID: {resolved.pid}</span>}
            {resolved.heartbeatTs != null && (
              <span>
                {t("心跳", "Heartbeat")}: {formatTime(resolved.heartbeatTs)}
              </span>
            )}
          </div>
        )}

        <p className="paper-proc-note">
          {t(
            "浏览器形态仅展示模拟盘状态，不直接拉起本地进程。请在终端或桌面工作台运行：",
            "Browser mode displays paper process status only. Start the account process in your terminal or Desktop Workbench:",
          )}
        </p>

        <div className="paper-proc-cli-box">
          <code className="paper-proc-cli-code" data-testid="paper-cli-command">
            {cliCommand}
          </code>
          <button
            type="button"
            className="ghost-button paper-proc-copy-btn"
            data-testid="paper-cli-copy-btn"
            onClick={() => void handleCopyCli()}
          >
            {copied ? t("已复制", "Copied") : t("复制命令", "Copy")}
          </button>
        </div>
      </section>
    );
  }

  const isManagedRunning = managedProcess?.state === "RUNNING";
  const isExternalRunning = resolved.phase === "ONLINE" && resolved.origin === "EXTERNAL";
  const isCrashed = resolved.phase === "CRASHED";
  const canStart =
    Boolean(selectedScheme?.valid) && !isManagedRunning && !isExternalRunning && busyAction === null;
  const canStop = isManagedRunning && busyAction === null;
  const canRestart =
    Boolean(selectedScheme?.valid) && (isManagedRunning || isCrashed) && busyAction === null;
  const canOpenLog = Boolean(managedProcess?.log_path) && busyAction === null;

  return (
    <section
      className="paper-proc-card paper-proc-card--desktop"
      data-testid="paper-process-control"
      data-mode="desktop"
      aria-label={t("模拟盘进程托管", "Paper Account Process Control")}
    >
      <div className="paper-proc-head">
        <div className="paper-proc-title-wrap">
          <h3>{t("模拟盘进程", "Paper Process")}</h3>
          <span className={statusBadgeClass(resolved.phase)} data-testid="paper-proc-status">
            {statusBadgeLabel(resolved)}
          </span>
        </div>
        <button
          type="button"
          className="ghost-button"
          data-testid="paper-schemes-refresh-btn"
          disabled={loading || busyAction !== null}
          onClick={() => void loadDesktopState(true)}
        >
          {t("刷新方案", "Refresh")}
        </button>
      </div>

      {loading && schemes.length === 0 ? (
        <p className="paper-proc-note" role="status">
          {t("正在扫描交易方案…", "Scanning trading schemes…")}
        </p>
      ) : schemes.length === 0 ? (
        <div className="paper-proc-empty" data-testid="paper-schemes-empty">
          <p className="paper-proc-empty-title">
            {t("未发现可用的模拟盘交易方案", "No trading schemes found")}
          </p>
          <p className="paper-proc-note">
            {t(
              "请将交易方案 YAML 文件（.yaml / .yml）放入以下目录后点击“刷新方案”：",
              "Place trading scheme YAML files (.yaml / .yml) in the directory below, then click Refresh:",
            )}
          </p>
          <code className="paper-proc-path-hint" data-testid="paper-schemes-dir">
            {schemesDir || `${resolvedDataDir}/schemes`}
          </code>
        </div>
      ) : (
        <>
          <label className="paper-proc-select-row">
            <span>{t("交易方案", "Trading scheme")}</span>
            <select
              className="paper-proc-select"
              data-testid="paper-scheme-select"
              value={selectedSchemePath}
              disabled={busyAction !== null}
              onChange={(event) => {
                setSelectedSchemePath(event.target.value);
                setActionError(null);
              }}
            >
              {schemes.map((item) => (
                <option key={item.path} value={item.path}>
                  {item.valid ? "" : "⚠ "}
                  {item.scheme}
                  {item.sub_accounts.length > 0 ? ` (${item.sub_accounts.join(", ")})` : ""}
                </option>
              ))}
            </select>
          </label>

          {selectedScheme && !selectedScheme.valid && (
            <div
              className="paper-proc-error"
              role="alert"
              data-testid="paper-scheme-validation-error"
            >
              <strong>{t("方案校验未通过", "Scheme validation failed")}:</strong>{" "}
              <span>
                {selectedScheme.validation_error ??
                  t("请检查方案配置或策略批准状态。", "Check scheme YAML or strategy approval.")}
              </span>
            </div>
          )}

          {(resolved.pid != null || resolved.heartbeatTs != null || resolved.exitCode != null) && (
            <div className="paper-proc-meta" data-testid="paper-proc-meta">
              {resolved.pid != null && <span>PID: {resolved.pid}</span>}
              {resolved.heartbeatTs != null && (
                <span>
                  {t("心跳", "Heartbeat")}: {formatTime(resolved.heartbeatTs)}
                </span>
              )}
              {resolved.phase === "CRASHED" && resolved.exitCode != null && (
                <span>
                  {t("退出码", "Exit code")}: {resolved.exitCode}
                </span>
              )}
            </div>
          )}

          <div className="paper-proc-actions">
            <button
              type="button"
              className="primary-button"
              data-testid="paper-start-btn"
              disabled={!canStart}
              onClick={() => void handleStart()}
            >
              {busyAction === "start"
                ? t("启动中…", "Starting…")
                : t("启动模拟盘", "Start Paper")}
            </button>

            <button
              type="button"
              className="ghost-button"
              data-testid="paper-stop-btn"
              disabled={!canStop}
              onClick={() => void handleStop()}
            >
              {busyAction === "stop" ? t("停止中…", "Stopping…") : t("停止", "Stop")}
            </button>

            <button
              type="button"
              className={`ghost-button${isCrashed ? " paper-proc-btn--highlight" : ""}`}
              data-testid="paper-restart-btn"
              disabled={!canRestart}
              onClick={() => void handleRestart()}
            >
              {busyAction === "restart" ? t("重启中…", "Restarting…") : t("重启", "Restart")}
            </button>

            <button
              type="button"
              className="ghost-button"
              data-testid="paper-open-log-btn"
              disabled={!canOpenLog}
              onClick={() => void handleOpenLog()}
            >
              {t("打开日志", "Open Log")}
            </button>
          </div>
        </>
      )}

      {displayError && (
        <div className="paper-proc-error" role="alert" data-testid="paper-process-error">
          <div className="paper-proc-error-head">
            <strong>
              {resolved.phase === "CRASHED"
                ? t("账户进程异常退出", "Account process crashed")
                : t("启动或操作失败", "Process action failed")}
            </strong>
            {actionError && (
              <button
                type="button"
                className="icon-button paper-proc-error-dismiss"
                aria-label={t("关闭错误提示", "Dismiss error")}
                onClick={() => setActionError(null)}
              >
                ×
              </button>
            )}
          </div>
          <p className="paper-proc-error-body">{displayError}</p>
        </div>
      )}

      <label className="paper-proc-keep-alive">
        <input
          type="checkbox"
          data-testid="paper-keep-alive-toggle"
          checked={keepAlive}
          onChange={(event) => void handleToggleKeepAlive(event.target.checked)}
        />
        <span>
          {t(
            "关闭工作台时保持模拟盘后台运行",
            "Keep paper accounts running after closing workbench",
          )}
        </span>
      </label>
    </section>
  );
}
