import {
  configureApi, normalizeLocale, PANELS, panelLabel, panelPath, setLocale, useLocale, WorkbenchPanel,
  type PanelName, type Theme,
} from "@talaria/desk-ui";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ComponentType, KeyboardEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import styles from "@talaria/desk-ui/styles.css?inline";
import hostStyles from "./styles.css?inline";
import { useCopy } from "./copy";
import { createLinkRelay } from "./linkRelay";
import { resolveHostText, syncHostLocale } from "./localeSync.mjs";
import { MESSAGES } from "./messages.mjs";
import { dockPanels, panelLinkKey, restoreActivePanel } from "./panelPreferences.mjs";
import { MAIN_PANEL_ID, RIGHTBAR_TABS, registerPanels } from "./registerPanels.mjs";
import {
  nextTabIndex, panelDomId, readDockPreference, readWorkspaceTab,
  saveDockPanel, saveWorkspaceTab, tabDomId,
} from "./workspaceTabs.mjs";

const STYLE_ID = "talaria-desk-plugin-styles";
const SERVICE_OFFLINE = "talaria-service-offline";
const CONNECTION_REFRESH_MS = 3_000;
const DOCK_PANELS = dockPanels(PANELS) as Exclude<PanelName, "chart">[];
type DockPanel = (typeof DOCK_PANELS)[number];
type Connection = { baseUrl: string; relay: ReturnType<typeof createLinkRelay> };

interface Observable<Value> {
  getSnapshot(): Value;
  subscribe(listener: () => void): () => void;
}

interface LocaleHost {
  getSnapshot(): { active: string };
  subscribe(listener: () => void): () => void;
  resolveText?(text: { zh: string; en: string }): string;
}

// Public client contracts from ui-layout, ui-sidebar, ui-sidebar-right and
// dsh-client-locale in DSH 0.2.2-rc.5.
interface SlotContext {
  effect(callback: () => () => void): void;
  locale: LocaleHost;
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

function browserStorage(kind: "sessionStorage" | "localStorage"): Storage {
  return window[kind];
}

function createConnection() {
  const controller = new AbortController();
  let pending: Promise<Connection> | undefined;
  let active: Connection | undefined;

  async function refresh(): Promise<Connection> {
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(CONNECTION_REFRESH_MS)]);
    try {
      const response = await fetch("/talaria/bootstrap", { signal, cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const { base_url: baseUrl, token } = await response.json();
      if (typeof baseUrl !== "string" || !baseUrl || typeof token !== "string" || !token) {
        throw new Error("connection credentials are missing");
      }
      const health = await fetch(new URL("/api/health", baseUrl), { signal, cache: "no-store" });
      if (!health.ok) throw new Error(`HTTP ${health.status}`);
      signal.throwIfAborted();
      configureApi({ baseUrl, token });
      if (active?.baseUrl !== baseUrl) {
        active?.relay.close();
        active = { baseUrl, relay: createLinkRelay(baseUrl) };
      }
      return active;
    } catch (error) {
      throw new Error(SERVICE_OFFLINE, { cause: error });
    }
  }

  function get() {
    pending ??= refresh().finally(() => { pending = undefined; });
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

function SurfaceBody({
  connection, children,
}: { connection: ReturnType<typeof createConnection>; children: (value: Connection) => ReactNode }) {
  const copy = useCopy();
  const [value, setValue] = useState<Connection>();
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    async function refresh() {
      try {
        const result = await connection.get();
        if (!cancelled) {
          setValue(result);
          setError(false);
        }
      } catch {
        if (!cancelled) {
          setValue(undefined);
          setError(true);
        }
      } finally {
        if (!cancelled) timer = window.setTimeout(refresh, CONNECTION_REFRESH_MS);
      }
    }
    void refresh();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [connection, attempt]);

  // The relay iframe is recreated when the service address changes.
  useEffect(() => {
    value?.relay.setTitle(copy.linkTitle);
  }, [value, copy.linkTitle]);

  return value ? <>{children(value)}</> : (
    <div className="desk-connect">
      {error ? (
        <>
          <p role="alert">{copy.offline}</p>
          <button type="button" className="ghost-button" onClick={() => {
            setError(false);
            setAttempt((count) => count + 1);
          }}>{copy.retry}</button>
        </>
      ) : <p role="status">{copy.connecting}</p>}
    </div>
  );
}

/**
 * One connection boundary. `Workspace` keeps its tab bar outside this
 * component so the five tabs stay usable while the service is down; the
 * session tab keeps the themed Surface wrapper.
 */
function Surface({ connection, children }: {
  connection: ReturnType<typeof createConnection>;
  children: (value: Connection, theme: Theme) => ReactNode;
}) {
  const theme = useHostTheme();
  return (
    <div className="talaria-desk talaria-dsh-surface" data-theme={theme}>
      <SurfaceBody connection={connection}>{(value) => children(value, theme)}</SurfaceBody>
    </div>
  );
}

const ICONS: Record<PanelName, string> = {
  chart: "M4 17l4-5 4 2 7-8M4 4v16h16",
  alerts: "M6 16h12l-2-3V9a4 4 0 00-8 0v4l-2 3zm4 3h4",
  scanner: "M4 9V4h5m6 0h5v5m0 6v5h-5m-6 0H4v-5m3-3h10",
  accounts: "M3 6h18v13H3z M3 10h18m-7 5h4",
  levels: "M4 6h16M4 12h12M4 18h10",
};

/** A prominent winged Talaria star for the single sidebar row. */
function BrandIcon({ size = 20, active = false }: { size?: number; active?: boolean }) {
  return (
    <svg className={`talaria-dsh-brand${active ? " is-active" : ""}`} aria-hidden="true"
      viewBox="0 0 24 24" width={size} height={size}>
      <path d="M12 1.7l2.8 6.4 6.8.6-5.1 4.5 1.5 6.7L12 16.2l-6 3.7 1.5-6.7L2.4 8.7l6.8-.6z"
        fill="currentColor" />
      <path d="M5.2 5.1c3.1.2 5.5 1.5 6.8 3.8M18.8 5.1c-3.1.2-5.5 1.5-6.8 3.8"
        fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" opacity=".62" />
    </svg>
  );
}

function PanelIcon({ panel, size = 16 }: { panel: PanelName; size?: number }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" width={size} height={size}
      fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d={ICONS[panel]} />
    </svg>
  );
}

function ChartWithDock({
  connection, ctx, theme, onOpenTab,
}: {
  connection: Connection;
  ctx: SlotContext;
  theme: Theme;
  onOpenTab: (panel: PanelName) => void;
}) {
  const copy = useCopy();
  const locale = useLocale();
  const [panel, setPanel] = useState<DockPanel | null>(() =>
    readDockPreference(browserStorage, DOCK_PANELS) as DockPanel | null,
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
    saveDockPanel(next ?? "");
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
              {copy.utilityDock}
            </button>
          }
        />
      </div>
      {open && <div ref={inlineDock} className="talaria-dsh-dock-inline" />}
      {panel && createPortal(
        <aside className="talaria-desk talaria-dsh-dock" data-theme={theme} aria-label={copy.dockAria}>
          <div className="talaria-dsh-dock-bar">
            {DOCK_PANELS.map((item) => (
              <button type="button" key={item} className="ghost-button" aria-pressed={panel === item}
                onClick={() => select(item)}>{panelLabel(item, locale)}</button>
            ))}
            <button type="button" className="ghost-button" aria-label={copy.closeDock} onClick={() => select(null)}>×</button>
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
                  onClick={() => onOpenTab(panel)}>{copy.openMain}</button>
                <button type="button" className="ghost-button"
                  onClick={() => {
                    const url = new URL(panelPath(panel), connection.baseUrl);
                    url.searchParams.set("lang", locale);
                    window.open(url, "_blank", "popup,noopener,width=560,height=710");
                  }}>
                  {copy.openWindow}
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

function WorkspaceView({
  connection, ctx, theme,
}: { connection: ReturnType<typeof createConnection>; ctx: SlotContext; theme: Theme }) {
  const copy = useCopy();
  const locale = useLocale();
  const [tab, setTab] = useState<PanelName>(() => readWorkspaceTab(browserStorage, PANELS));
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  // Also writes the migrated legacy choice on first mount.
  useEffect(() => {
    saveWorkspaceTab(tab);
  }, [tab]);

  function selectTab(next: PanelName) {
    setTab(next);
    saveWorkspaceTab(next);
  }

  function focusTab(index: number) {
    const next = PANELS[index];
    if (!next) return;
    selectTab(next);
    tabRefs.current[index]?.focus();
  }

  function openTab(panel: PanelName) {
    focusTab(PANELS.indexOf(panel));
  }

  function dockToChart(panel: PanelName) {
    saveDockPanel(panel);
    focusTab(PANELS.indexOf("chart"));
  }

  function onTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next = nextTabIndex(event.key, index, PANELS.length) as number | null;
    if (next === null) return;
    event.preventDefault();
    focusTab(next);
  }

  return (
    <div className="talaria-dsh-workspace">
      <header className="talaria-dsh-workspace-head">
        <BrandIcon size={22} />
        <span>{copy.workspace}</span>
      </header>
      <div className="talaria-dsh-tabs" role="tablist" aria-label={copy.workspaceTabs}>
        {PANELS.map((panel, index) => {
          const selected = panel === tab;
          return (
            <button
              key={panel}
              ref={(node) => { tabRefs.current[index] = node; }}
              type="button"
              role="tab"
              id={tabDomId(panel)}
              className="talaria-dsh-tab"
              aria-selected={selected}
              aria-controls={panelDomId(panel)}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectTab(panel)}
              onKeyDown={(event) => onTabKeyDown(event, index)}
            >
              <PanelIcon panel={panel} />
              <span>{panelLabel(panel, locale)}</span>
            </button>
          );
        })}
      </div>
      <div role="tabpanel" id={panelDomId(tab)} aria-labelledby={tabDomId(tab)}
        tabIndex={0} className="talaria-dsh-tab-panel">
        <SurfaceBody connection={connection}>
          {(value) => tab === "chart" ? (
            <ChartWithDock connection={value} ctx={ctx} theme={theme} onOpenTab={openTab} />
          ) : (
            <WorkbenchPanel key={tab} panel={tab} theme={theme} transport={value.relay}
              persistenceKey={panelLinkKey(tab, "main")}
              tools={
                <button type="button" className="ghost-button" onClick={() => dockToChart(tab)}>
                  {copy.dockToChart}
                </button>
              }
            />
          )}
        </SurfaceBody>
      </div>
      {PANELS.filter((panel) => panel !== tab).map((panel) => (
        <div key={panel} role="tabpanel" id={panelDomId(panel)} aria-labelledby={tabDomId(panel)} hidden />
      ))}
    </div>
  );
}

function Workspace({
  connection, ctx,
}: { connection: ReturnType<typeof createConnection>; ctx: SlotContext }) {
  // The tab bar and brand header live outside the connection boundary, so an
  // offline service only replaces the selected tab's content.
  const theme = useHostTheme();
  return (
    <div className="talaria-desk talaria-dsh-surface" data-theme={theme}>
      <WorkspaceView connection={connection} ctx={ctx} theme={theme} />
    </div>
  );
}

function hostPanelLabel(ctx: SlotContext, panel: PanelName) {
  return panelLabel(panel, normalizeLocale(ctx.locale.getSnapshot().active));
}

export const inject = ["slots", "layout", "sidebarRight", "sidebarRightTabs", "locale"];

export function apply(ctx: SlotContext): void {
  const connection = createConnection();

  // desk-ui components subscribe to its store; host thunks read ctx.locale
  // directly so labels never wait for a second notification.
  ctx.effect(() => syncHostLocale(ctx.locale, locale => setLocale(locale), normalizeLocale));

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

  registerPanels(ctx, {
    mainId: MAIN_PANEL_ID,
    sidebarLabel: () => resolveHostText(ctx.locale, MESSAGES.workspace),
    tabs: RIGHTBAR_TABS.map((tab) => ({
      panel: tab.panel as PanelName,
      kind: tab.kind,
      title: () => tab.panel === "chart"
        ? resolveHostText(ctx.locale, MESSAGES.chartTab)
        : `Talaria ${hostPanelLabel(ctx, tab.panel as PanelName)}`,
      ...(tab.panel === "chart" && {
        description: () => resolveHostText(ctx.locale, MESSAGES.guideDescription),
      }),
    })),
  }, {
    main: () => function MainPanel() {
      return <Workspace connection={connection} ctx={ctx} />;
    },
    sidebarIcon: () => BrandIcon,
    guideIcon: (panel: PanelName) => function GuideIcon({ size }: { size?: number }) {
      return <PanelIcon panel={panel} size={size} />;
    },
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

  // Open the chart automatically in the first Talaria consultation only.
  ctx.effect(() => {
    let opened = false;
    let frame = 0;
    function schedule() {
      cancelAnimationFrame(frame);
      const session = ctx.sidebarRight.mounted.getSnapshot();
      if (opened || !session?.startsWith("talaria-consult-")) return;
      let attempts = 0;
      function openTab() {
        if (ctx.sidebarRight.mounted.getSnapshot() !== session) return;
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
