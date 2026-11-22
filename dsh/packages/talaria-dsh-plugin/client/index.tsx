import {
  configureApi, PANELS, PANEL_LABELS, panelPath, WorkbenchPanel,
  type PanelName, type Theme,
} from "@talaria/desk-ui";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import { createPortal } from "react-dom";
import styles from "@talaria/desk-ui/styles.css?inline";
import hostStyles from "./styles.css?inline";
import { createLinkRelay } from "./linkRelay";
import { panelLinkKey, restoreActivePanel } from "./panelPreferences.mjs";
import { panelId, registerPanels } from "./registerPanels.mjs";

const STYLE_ID = "talaria-desk-plugin-styles";
const DOCK_KEY = "talaria-dsh-docked-panel";
const DOCK_PANELS = ["alerts", "scanner", "levels"] as const;
type DockPanel = (typeof DOCK_PANELS)[number];
type Connection = { baseUrl: string; relay: ReturnType<typeof createLinkRelay> };

interface Observable<Value> {
  getSnapshot(): Value;
  subscribe(listener: () => void): () => void;
}

// Public client contracts from ui-layout, ui-sidebar and ui-sidebar-right in DSH 0.2.2-rc.3.
interface SlotContext {
  effect(callback: () => () => void): void;
  slots: {
    inject(name: string, register: () => (() => void) | Generator<() => void>): () => void;
    register(options: Record<string, unknown>, component: ComponentType): () => void;
  };
  layout: {
    panelInfo: Observable<{ activePanelId: string | null }>;
    selectPanel(id: string | null): void;
    openRightbar(track: boolean, fullscreen: boolean): void;
    closeRightbar(): void;
  };
  sidebarRightTabs: {
    register(definition: Record<string, unknown>): () => void;
  };
  sidebarRight: {
    mounted: Observable<string | undefined>;
    openTab(kind: string): void;
  };
}

function readPreference(key: string) {
  try {
    return sessionStorage.getItem(key) ?? localStorage.getItem(key);
  } catch {
    return null;
  }
}

function savePreference(key: string, value: string) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // A restricted storage context can still use each panel.
  }
}

function createConnection() {
  const controller = new AbortController();
  let pending: Promise<Connection> | undefined;
  let active: Connection | undefined;

  function get() {
    pending ??= fetch("/talaria/bootstrap", { signal: controller.signal, cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`连接凭据读取失败（HTTP ${response.status}）`);
        const { base_url: baseUrl, token } = await response.json();
        if (typeof baseUrl !== "string" || !baseUrl || typeof token !== "string" || !token) {
          throw new Error("连接凭据缺少服务地址或令牌");
        }
        controller.signal.throwIfAborted();
        configureApi({ baseUrl, token });
        active = { baseUrl, relay: createLinkRelay(baseUrl) };
        return active;
      })
      .catch((error: unknown) => {
        pending = undefined;
        throw error;
      });
    return pending;
  }

  return {
    get,
    close() {
      controller.abort();
      active?.relay.close();
    },
  };
}

function useHostTheme(): Theme {
  const read = () => document.body.hasAttribute("data-ds-dark-theme") ? "dark" : "light";
  const [theme, setTheme] = useState<Theme>(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(read()));
    observer.observe(document.body, { attributeFilter: ["data-ds-dark-theme"] });
    setTheme(read());
    return () => observer.disconnect();
  }, []);
  return theme;
}

function Surface({
  connection, children,
}: { connection: ReturnType<typeof createConnection>; children: (value: Connection, theme: Theme) => ReactNode }) {
  const theme = useHostTheme();
  const [value, setValue] = useState<Connection>();
  const [error, setError] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    connection.get().then((result) => {
      if (!cancelled) setValue(result);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "无法连接常驻服务");
    });
    return () => { cancelled = true; };
  }, [connection, attempt]);

  return (
    <div className="talaria-desk talaria-dsh-surface" data-theme={theme}>
      {value ? children(value, theme) : (
        <div className="desk-connect">
          {error ? (
            <>
              <p role="alert">{error}</p>
              <button type="button" className="ghost-button" onClick={() => {
                setError(undefined);
                setAttempt((count) => count + 1);
              }}>重试</button>
            </>
          ) : <p role="status">正在连接 Talaria 常驻服务…</p>}
        </div>
      )}
    </div>
  );
}

function ChartWithDock({
  connection, ctx, theme,
}: { connection: Connection; ctx: SlotContext; theme: Theme }) {
  const [panel, setPanel] = useState<DockPanel | null>(() =>
    DOCK_PANELS.find((item) => readPreference(DOCK_KEY) === item) ?? null,
  );
  const inlineDock = useRef<HTMLDivElement>(null);
  const [seat] = useState(() => document.createElement("div"));
  const open = panel !== null;

  useLayoutEffect(() => {
    if (!open) return;
    // A global main panel hides the Session rightbar. Reuse AppFrame's
    // resizable track and keep its Session occupant untouched.
    const track = document.querySelector<HTMLElement>("[data-rightbar-col]");
    function place() {
      const dock = track && track.clientWidth >= 260 ? track : inlineDock.current;
      seat.className = dock === track ? "talaria-dsh-dock-seat is-in-track" : "talaria-dsh-dock-seat";
      if (dock && dock !== seat.parentElement) dock.append(seat);
    }
    ctx.layout.openRightbar(true, false);
    place();
    const observer = new ResizeObserver(place);
    if (track) observer.observe(track);
    return () => {
      observer.disconnect();
      seat.remove();
      ctx.layout.closeRightbar();
    };
  }, [ctx, open, seat]);

  function select(next: DockPanel | null) {
    savePreference(DOCK_KEY, next ?? "");
    setPanel(next);
  }

  return (
    <>
      <div className="talaria-dsh-chart">
        <WorkbenchPanel
          panel="chart"
          persistenceKey={panelLinkKey("chart", "main")}
          theme={theme}
          transport={connection.relay}
          embedded
          tools={
            <button type="button" className="ghost-button" aria-expanded={open}
              onClick={() => select(open ? null : "alerts")}>
              功能右栏
            </button>
          }
        />
      </div>
      {open && <div ref={inlineDock} className="talaria-dsh-dock-inline" />}
      {panel && createPortal(
        <aside className="talaria-desk talaria-dsh-dock" data-theme={theme} aria-label="Talaria 功能右栏">
          <div className="talaria-dsh-dock-bar">
            {DOCK_PANELS.map((item) => (
              <button type="button" key={item} className="ghost-button" aria-pressed={panel === item}
                onClick={() => select(item)}>{PANEL_LABELS[item]}</button>
            ))}
            <button type="button" className="ghost-button" aria-label="关闭功能右栏" onClick={() => select(null)}>×</button>
          </div>
          <WorkbenchPanel
            key={panel}
            panel={panel}
            persistenceKey={panelLinkKey(panel, "dock")}
            theme={theme}
            transport={connection.relay}
            tools={
              <div className="talaria-dsh-dock-actions">
                <button type="button" className="ghost-button"
                  onClick={() => ctx.layout.selectPanel(panelId(panel))}>在主区打开</button>
                <button type="button" className="ghost-button"
                  onClick={() => window.open(new URL(panelPath(panel), connection.baseUrl), "_blank", "popup,noopener,width=560,height=710")}>
                  独立窗口
                </button>
              </div>
            }
          />
        </aside>,
        seat,
      )}
    </>
  );
}

const ICONS: Record<PanelName, string> = {
  chart: "M4 17l4-5 4 2 7-8M4 4v16h16",
  alerts: "M6 16h12l-2-3V9a4 4 0 00-8 0v4l-2 3zm4 3h4",
  scanner: "M4 9V4h5m6 0h5v5m0 6v5h-5m-6 0H4v-5m3-3h10",
  levels: "M4 6h16M4 12h12M4 18h10",
};

export const inject = ["slots", "layout", "sidebarRight", "sidebarRightTabs"];

export function apply(ctx: SlotContext): void {
  const connection = createConnection();
  ctx.effect(() => {
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = styles + hostStyles;
    document.head.append(style);
    return () => {
      connection.close();
      style.remove();
    };
  });

  registerPanels(ctx, PANEL_LABELS, {
    main: (panel: PanelName) => function MainPanel() {
      return (
        <Surface connection={connection}>
          {(value, theme) => panel === "chart"
            ? <ChartWithDock connection={value} ctx={ctx} theme={theme} />
            : <WorkbenchPanel panel={panel} theme={theme} transport={value.relay} persistenceKey={panelLinkKey(panel, "main")}
                tools={<button type="button" className="ghost-button" onClick={() => {
                  savePreference(DOCK_KEY, panel);
                  ctx.layout.selectPanel(panelId("chart"));
                }}>停靠图表右栏</button>} />}
        </Surface>
      );
    },
    sidebarIcon: (panel: PanelName) => ({ size = 16 }: { size?: number }) => (
      <svg aria-hidden="true" viewBox="0 0 24 24" width={size} height={size}
        fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d={ICONS[panel]} />
      </svg>
    ),
    sessionTab: (panel: PanelName) => function SessionTab({ sessionId }: { sessionId: string }) {
      return (
        <Surface connection={connection}>
          {(value, theme) => <WorkbenchPanel key={sessionId} panel={panel} persistenceKey={panelLinkKey(panel, "session", sessionId)}
            theme={theme} transport={value.relay} embedded fullWorkbench={panel === "chart"} />}
        </Surface>
      );
    },
    onMainRegistered() {
      return restoreActivePanel(ctx.layout, PANELS);
    },
  });

  // Preserve the consultation chart's one-time opening when a Session seat is available.
  ctx.effect(() => {
    let opened = false;
    let frame = 0;
    function schedule() {
      cancelAnimationFrame(frame);
      if (opened || ctx.sidebarRight.mounted.getSnapshot() === undefined) return;
      let attempts = 0;
      function openTab() {
        if (ctx.sidebarRight.mounted.getSnapshot() === undefined) return;
        try {
          ctx.sidebarRight.openTab("talaria-desk");
          opened = true;
        } catch {
          if (++attempts < 40) frame = requestAnimationFrame(openTab);
        }
      }
      frame = requestAnimationFrame(openTab);
    }
    const unsubscribe = ctx.sidebarRight.mounted.subscribe(schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      unsubscribe();
    };
  });
}
