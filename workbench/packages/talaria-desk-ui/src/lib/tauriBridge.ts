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
