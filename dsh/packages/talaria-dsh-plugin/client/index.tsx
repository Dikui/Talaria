import { DeskWorkbench, configureApi } from "@talaria/desk-ui";
import { useEffect, useState } from "react";
import type { ReactElement } from "react";
import styles from "@talaria/desk-ui/styles.css?inline";

const STYLE_ID = "talaria-desk-plugin-styles";

function ensureStyles(): void {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) {
    return;
  }
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = styles;
  document.head.append(style);
}

function removeStyles(): void {
  document.getElementById(STYLE_ID)?.remove();
}

interface BootstrapPayload {
  base_url?: string;
  token?: string;
  message?: string;
}

function TalariaDeskPanel(): ReactElement {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ensureStyles();
    const controller = new AbortController();
    fetch("/talaria/bootstrap", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`bootstrap HTTP ${response.status}`);
        }
        return (await response.json()) as BootstrapPayload;
      })
      .then((payload) => {
        if (!payload.base_url || !payload.token) {
          throw new Error("bootstrap 缺少 base_url 或 token");
        }
        configureApi({ baseUrl: payload.base_url, token: payload.token });
        setReady(true);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => controller.abort();
  }, []);

  if (error) {
    return (
      <div className="talaria-desk-panel talaria-desk-panel--error">
        Talaria 守护进程未连接（{error}）。请先运行 `talaria up`。
      </div>
    );
  }
  if (!ready) {
    return <div className="talaria-desk-panel">正在连接 Talaria 守护进程…</div>;
  }
  return (
    <div className="talaria-desk-panel">
      <DeskWorkbench embedded />
    </div>
  );
}

export const inject = ["slots", "sidebarRight", "sidebarRightTabs"];

export interface SlotContext {
  effect(callback: () => () => void): () => void;
  slots: {
    inject(name: string, register: () => () => void): () => void;
    register(options: Record<string, unknown>, component: () => React.ReactElement): () => void;
  };
  sidebarRightTabs: {
    register(definition: Record<string, unknown>): () => void;
  };
  sidebarRight: {
    openTab(kind: string, options?: Record<string, unknown>): void;
  };
}

export function apply(ctx: SlotContext): void {
  ctx.effect(() => {
    const disposeTab = ctx.sidebarRightTabs.register({
      id: "talaria-desk-tab",
      kind: "talaria-desk",
      title: () => "Talaria 看盘",
      keepMounted: true,
      canOpen: () => true,
      guide: [
        {
          id: "talaria-desk",
          order: 10,
          title: () => "Talaria 看盘",
          description: () => "在右侧面板打开确定性历史行情与指标工作台",
        },
      ],
    });
    const disposeBody = ctx.slots.inject("sidebar.right.pane.tab", () =>
      ctx.slots.register(
        { name: "sidebar.right.pane.tab", key: "talaria-desk-tab", locale: "talaria-desk" },
        TalariaDeskPanel,
      ),
    );
    return () => {
      disposeBody();
      disposeTab();
      removeStyles();
    };
  });

  // 首次出现会话时打开一次；用户关闭后不再抢占。
  ctx.effect(() => {
    let done = false;
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (done || attempts > 40) {
        window.clearInterval(timer);
        return;
      }
      try {
        ctx.sidebarRight.openTab("talaria-desk");
        done = true;
        window.clearInterval(timer);
      } catch {
        // 右侧栏或会话尚未就绪；继续等待。
      }
    }, 1500);
    return () => window.clearInterval(timer);
  });
}
