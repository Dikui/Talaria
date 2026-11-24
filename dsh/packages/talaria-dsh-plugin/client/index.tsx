import {
  configureApi, normalizeLocale, PANELS, panelLabel, setLocale, WorkbenchPanel,
  type PanelName, type Theme,
} from "@talaria/desk-ui";
import { useEffect, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import styles from "@talaria/desk-ui/styles.css?inline";
import hostStyles from "./styles.css?inline";
import { useCopy } from "./copy";
import { createLinkRelay } from "./linkRelay";
import { resolveHostText, syncHostLocale } from "./localeSync.mjs";
import { MESSAGES } from "./messages.mjs";
import { panelLinkKey } from "./panelPreferences.mjs";
import { RIGHTBAR_TABS, registerPanels } from "./registerPanels.mjs";

const STYLE_ID = "talaria-desk-plugin-styles";
const SERVICE_OFFLINE = "talaria-service-offline";
const CONNECTION_REFRESH_MS = 3_000;
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

// Public client contracts from ui-sidebar-right and dsh-client-locale in DSH 0.2.2-rc.5.
interface SlotContext {
  effect(callback: () => () => void): void;
  locale: LocaleHost;
  slots: {
    inject(name: string, register: () => (() => void) | Generator<() => void>): () => void;
    register(options: Record<string, unknown>, component: ComponentType): () => void;
  };
  sidebarRightTabs: {
    register(definition: Record<string, unknown>): () => void;
  };
  sidebarRight: {
    mounted: Observable<string | undefined>;
    openTab(kind: string): void;
  };
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

function hostPanelLabel(ctx: SlotContext, panel: PanelName) {
  return panelLabel(panel, normalizeLocale(ctx.locale.getSnapshot().active));
}

export const inject = ["slots", "sidebarRight", "sidebarRightTabs", "locale"];

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
    sidebarIcon: () => BrandIcon,
    guideIcon: (panel: PanelName) => function GuideIcon({ size }: { size?: number }) {
      return <PanelIcon panel={panel} size={size} />;
    },
    sessionTab: (panel: PanelName) => function SessionTab({ sessionId }: { sessionId: string }) {
      return (
        <Surface connection={connection}>
          {(value, theme) => <WorkbenchPanel key={sessionId} panel={panel} persistenceKey={panelLinkKey(panel, "session", sessionId)}
            theme={theme} transport={value.relay} embedded fullWorkbench={false} />}
        </Surface>
      );
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
