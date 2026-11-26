import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PaperAccountView } from "@talaria/contracts";

import { setLocale } from "../../lib/i18n";
import type { PaperProcessStatus, PaperSchemeList } from "../../lib/tauriBridge";
import {
  PaperProcessControl,
  buildPaperCliCommand,
  resolvePaperControlStatus,
} from "./PaperProcessControl";

function makeView(overrides: Partial<PaperAccountView> = {}): PaperAccountView {
  return {
    sub_account_id: "sa-main",
    positions: [],
    rounds: [],
    fills: [],
    ledger_seq: 3,
    process: {
      pid: 4242,
      scheme: "btc-paper",
      started_ts: 1_700_000_000_000,
      heartbeat_ts: 1_700_000_050_000,
      status: "RUNNING",
    },
    ...overrides,
  };
}

function mockTauriInvoke(
  handlers: Record<string, (args?: Record<string, unknown>) => unknown | Promise<unknown>>,
) {
  const invoke = vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    const fn = handlers[cmd];
    if (!fn) {
      throw new Error(`unexpected tauri invoke: ${cmd}`);
    }
    return fn(args);
  });
  window.__TAURI_INTERNALS__ = {
    invoke: invoke as <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>,
  };
  return invoke;
}

describe("Ticket 09: PaperProcessControl (desktop supervision & browser read-only mode)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setLocale("zh");
    delete window.__TAURI_INTERNALS__;
  });

  afterEach(() => {
    delete window.__TAURI_INTERNALS__;
  });

  it("renders browser mode with status and copyable CLI command, and NO start/stop buttons", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const view = makeView();
    render(<PaperProcessControl views={[view]} selectedView={view} dataDir="/tmp/talaria" />);

    const card = screen.getByTestId("paper-process-control");
    expect(card.getAttribute("data-mode")).toBe("browser");
    expect(screen.getByTestId("paper-proc-status").textContent).toContain("在线");
    expect(screen.queryByTestId("paper-start-btn")).toBeNull();
    expect(screen.queryByTestId("paper-stop-btn")).toBeNull();
    expect(screen.queryByTestId("paper-keep-alive-toggle")).toBeNull();

    const expectedCmd = buildPaperCliCommand({ scheme: "btc-paper", dataDir: "/tmp/talaria" });
    expect(screen.getByTestId("paper-cli-command").textContent).toBe(expectedCmd);

    fireEvent.click(screen.getByTestId("paper-cli-copy-btn"));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(expectedCmd);
      expect(screen.getByTestId("paper-cli-copy-btn").textContent).toBe("已复制");
    });
  });

  it("shows empty schemes guidance with directory convention in desktop mode", async () => {
    const emptySchemes: PaperSchemeList = {
      schemes_dir: "/var/tmp/talaria/data/schemes",
      data_dir: "/var/tmp/talaria/data",
      schemes: [],
    };
    mockTauriInvoke({
      list_paper_schemes: () => emptySchemes,
      list_paper_processes: () => [],
      get_paper_keep_alive: () => false,
    });

    render(<PaperProcessControl pollIntervalMs={0} />);

    await waitFor(() => {
      expect(screen.getByTestId("paper-schemes-empty")).toBeTruthy();
    });
    expect(screen.getByTestId("paper-schemes-dir").textContent).toBe(
      "/var/tmp/talaria/data/schemes",
    );
    expect(
      (screen.getByTestId("paper-keep-alive-toggle") as HTMLInputElement).checked,
    ).toBe(false);
  });

  it("supports start, stop, open log, and persisted keep-alive toggle in desktop mode", async () => {
    const schemes: PaperSchemeList = {
      schemes_dir: "/data/schemes",
      data_dir: "/data",
      schemes: [
        {
          scheme: "btc-paper",
          path: "/data/schemes/btc.yaml",
          file_name: "btc.yaml",
          sub_accounts: ["sa-main"],
          valid: true,
          validation_error: null,
        },
        {
          scheme: "agent-unapproved",
          path: "/data/schemes/agent.yaml",
          file_name: "agent.yaml",
          sub_accounts: ["sa-agent"],
          valid: false,
          validation_error: "agent_strategy.py：agent 策略未批准",
        },
      ],
    };

    let runningProcs: PaperProcessStatus[] = [];
    let keepAlive = false;

    const invoke = mockTauriInvoke({
      list_paper_schemes: () => schemes,
      list_paper_processes: () => runningProcs,
      get_paper_keep_alive: () => keepAlive,
      set_paper_keep_alive: (args) => {
        keepAlive = Boolean(args?.keepAlive);
        return keepAlive;
      },
      start_paper_account: (args) => {
        const started: PaperProcessStatus = {
          scheme: "btc-paper",
          scheme_path: String(args?.schemePath),
          sub_accounts: ["sa-main"],
          state: "RUNNING",
          pid: 9001,
          log_path: "/data/logs/account-btc-paper.log",
          started_at_ms: 1_700_000_000_000,
          exited_at_ms: null,
          exit_code: null,
          error_code: null,
          error_message: null,
        };
        runningProcs = [started];
        return started;
      },
      stop_paper_account: () => {
        const stopped: PaperProcessStatus = {
          ...runningProcs[0],
          state: "STOPPED",
          exited_at_ms: 1_700_000_060_000,
          exit_code: 0,
        };
        runningProcs = [stopped];
        return stopped;
      },
      open_paper_log: () => "/data/logs/account-btc-paper.log",
    });

    const onRefresh = vi.fn();
    render(<PaperProcessControl onRefreshAccounts={onRefresh} pollIntervalMs={0} />);

    await waitFor(() => {
      expect(screen.getByTestId("paper-scheme-select")).toBeTruthy();
    });

    // Switch to invalid scheme -> shows validation error & disables start.
    fireEvent.change(screen.getByTestId("paper-scheme-select"), {
      target: { value: "/data/schemes/agent.yaml" },
    });
    expect(screen.getByTestId("paper-scheme-validation-error").textContent).toContain(
      "agent 策略未批准",
    );
    expect((screen.getByTestId("paper-start-btn") as HTMLButtonElement).disabled).toBe(true);

    // Switch back to valid scheme & start.
    fireEvent.change(screen.getByTestId("paper-scheme-select"), {
      target: { value: "/data/schemes/btc.yaml" },
    });
    fireEvent.click(screen.getByTestId("paper-start-btn"));

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("start_paper_account", {
        schemePath: "/data/schemes/btc.yaml",
      });
      expect(screen.getByTestId("paper-proc-status").textContent).toBe("启动中");
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // Open log works once started.
    fireEvent.click(screen.getByTestId("paper-open-log-btn"));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("open_paper_log", {
        schemePath: "/data/schemes/btc.yaml",
      });
    });

    // Toggle keep-alive ON (spec D9).
    fireEvent.click(screen.getByTestId("paper-keep-alive-toggle"));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("set_paper_keep_alive", { keepAlive: true });
      expect(
        (screen.getByTestId("paper-keep-alive-toggle") as HTMLInputElement).checked,
      ).toBe(true);
    });

    // Stop managed process.
    fireEvent.click(screen.getByTestId("paper-stop-btn"));
    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("stop_paper_account", {
        schemePath: "/data/schemes/btc.yaml",
      });
      expect(screen.getByTestId("paper-proc-status").textContent).toBe("已停止");
    });
  });

  it("surfaces lock conflict and crash errors clearly and allows restarting crashed process", async () => {
    const schemes: PaperSchemeList = {
      schemes_dir: "/data/schemes",
      data_dir: "/data",
      schemes: [
        {
          scheme: "btc-paper",
          path: "/data/schemes/btc.yaml",
          file_name: "btc.yaml",
          sub_accounts: ["sa-main"],
          valid: true,
          validation_error: null,
        },
      ],
    };

    let procs: PaperProcessStatus[] = [
      {
        scheme: "btc-paper",
        scheme_path: "/data/schemes/btc.yaml",
        sub_accounts: ["sa-main"],
        state: "CRASHED",
        pid: 7777,
        log_path: "/data/logs/account-btc-paper.log",
        started_at_ms: 1_700_000_000_000,
        exited_at_ms: 1_700_000_010_000,
        exit_code: 2,
        error_code: "LOCK_CONFLICT",
        error_message:
          "该子账户已被其他进程占用 / Sub-account is already held by another process: sa-main",
      },
    ];

    mockTauriInvoke({
      list_paper_schemes: () => schemes,
      list_paper_processes: () => procs,
      get_paper_keep_alive: () => false,
      start_paper_account: () => {
        procs = [
          {
            ...procs[0],
            state: "RUNNING",
            pid: 8888,
            exited_at_ms: null,
            exit_code: null,
            error_code: null,
            error_message: null,
          },
        ];
        return procs[0];
      },
    });

    render(<PaperProcessControl pollIntervalMs={0} />);

    await waitFor(() => {
      expect(screen.getByTestId("paper-proc-status").textContent).toBe("已崩溃");
    });
    expect(screen.getByTestId("paper-process-error").textContent).toContain(
      "该子账户已被其他进程占用",
    );

    // Click Restart to recover from crash.
    fireEvent.click(screen.getByTestId("paper-restart-btn"));
    await waitFor(() => {
      expect(screen.getByTestId("paper-proc-status").textContent).toBe("启动中");
      expect(screen.queryByTestId("paper-process-error")).toBeNull();
    });
  });

  it("grounds online status in projection heartbeat and distinguishes managed vs external processes", () => {
    const scheme = {
      scheme: "btc-paper",
      path: "/data/schemes/btc.yaml",
      file_name: "btc.yaml",
      sub_accounts: ["sa-main"],
      valid: true,
      validation_error: null,
    };
    const runningView = makeView();

    // Projection running without Tauri handle -> ONLINE + EXTERNAL.
    const external = resolvePaperControlStatus([runningView], runningView, scheme, null);
    expect(external.phase).toBe("ONLINE");
    expect(external.origin).toBe("EXTERNAL");
    expect(external.pid).toBe(4242);

    // Projection offline + Tauri handle RUNNING -> STARTING (waiting for heartbeat).
    const offlineView = makeView({
      process: {
        pid: 4242,
        scheme: "btc-paper",
        started_ts: 1_700_000_000_000,
        heartbeat_ts: 1_700_000_001_000,
        status: "OFFLINE",
      },
    });
    const starting = resolvePaperControlStatus([offlineView], offlineView, scheme, {
      scheme: "btc-paper",
      scheme_path: "/data/schemes/btc.yaml",
      sub_accounts: ["sa-main"],
      state: "RUNNING",
      pid: 9999,
      log_path: "/data/logs/account-btc-paper.log",
      started_at_ms: 1_700_000_050_000,
      exited_at_ms: null,
      exit_code: null,
      error_code: null,
      error_message: null,
    });
    expect(starting.phase).toBe("STARTING");
    expect(starting.pid).toBe(9999);
  });
});
