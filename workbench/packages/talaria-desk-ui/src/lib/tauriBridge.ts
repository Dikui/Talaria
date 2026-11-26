export interface TauriServiceConnection {
  base_url: string;
  token: string;
  data_dir: string;
  managed: boolean;
  keep_running_in_background: boolean;
}

export interface TauriShellSettings {
  keep_running_in_background: boolean;
  managed: boolean;
  data_dir: string;
  base_url: string | null;
}

export interface PaperSchemeEntry {
  scheme: string;
  path: string;
  file_name: string;
  sub_accounts: string[];
  valid: boolean;
  validation_error: string | null;
}

export interface PaperSchemeList {
  schemes_dir: string;
  data_dir: string;
  schemes: PaperSchemeEntry[];
}

export type ManagedProcessState = "RUNNING" | "STOPPED" | "CRASHED" | "FAILED";

export interface PaperProcessStatus {
  scheme: string;
  scheme_path: string;
  sub_accounts: string[];
  state: ManagedProcessState;
  pid: number | null;
  log_path: string;
  started_at_ms: number | null;
  exited_at_ms: number | null;
  exit_code: number | null;
  error_code: string | null;
  error_message: string | null;
}

interface TauriInternals {
  invoke?: <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

declare global {
  interface Window {
    __TAURI_INTERNALS__?: TauriInternals;
    __TALARIA_BOOTSTRAP__?: {
      baseUrl?: string;
      base_url?: string;
      token?: string;
    };
  }
}

export function isTauriRuntime(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.__TAURI_INTERNALS__?.invoke === "function"
  );
}

export function isTauriDesktop(): boolean {
  return isTauriRuntime();
}

async function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!isTauriRuntime()) {
    return null;
  }
  const invoke = window.__TAURI_INTERNALS__?.invoke;
  if (!invoke) {
    return null;
  }
  return invoke<T>(cmd, args);
}

export async function getTauriServiceConnection(): Promise<TauriServiceConnection | null> {
  return tauriInvoke<TauriServiceConnection>("get_service_connection");
}

export async function getTauriShellSettings(): Promise<TauriShellSettings | null> {
  return tauriInvoke<TauriShellSettings>("get_shell_settings");
}

export async function setTauriKeepRunningInBackground(
  keepRunning: boolean,
): Promise<TauriShellSettings | null> {
  return tauriInvoke<TauriShellSettings>("set_keep_running_in_background", {
    keepRunning,
  });
}

export async function openTauriPopoutWindow(options: {
  urlPath: string;
  title: string;
  width?: number;
  height?: number;
}): Promise<boolean> {
  if (!isTauriRuntime()) {
    return false;
  }
  await tauriInvoke("open_popout_window", options);
  return true;
}

export async function listTauriPaperSchemes(): Promise<PaperSchemeList | null> {
  return tauriInvoke<PaperSchemeList>("list_paper_schemes");
}

export async function startTauriPaperAccount(
  schemePath: string,
): Promise<PaperProcessStatus | null> {
  return tauriInvoke<PaperProcessStatus>("start_paper_account", { schemePath });
}

export async function stopTauriPaperAccount(
  schemePath: string,
): Promise<PaperProcessStatus | null> {
  return tauriInvoke<PaperProcessStatus>("stop_paper_account", { schemePath });
}

export async function listTauriPaperProcesses(): Promise<PaperProcessStatus[] | null> {
  return tauriInvoke<PaperProcessStatus[]>("list_paper_processes");
}

export async function openTauriPaperLog(schemePath: string): Promise<string | null> {
  return tauriInvoke<string>("open_paper_log", { schemePath });
}

export async function getTauriPaperKeepAlive(): Promise<boolean | null> {
  return tauriInvoke<boolean>("get_paper_keep_alive");
}

export async function setTauriPaperKeepAlive(keepAlive: boolean): Promise<boolean | null> {
  return tauriInvoke<boolean>("set_paper_keep_alive", { keepAlive });
}
