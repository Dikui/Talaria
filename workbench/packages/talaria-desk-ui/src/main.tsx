import { useState } from "react";
import { createRoot } from "react-dom/client";
import "dockview-react/dist/styles/dockview.css";

import { initializeStandaloneApi } from "./lib/api";
import { getLocale, setLocale, t } from "./lib/i18n";
import { applyTheme, resolveInitialTheme } from "./lib/theme";
import { DEFAULT_INSTRUMENT, LINK_PATH, panelFromPath, type PanelName } from "./workbench/panels";
import { connectEmbeddedRelay } from "./workbench/transport";
import { WorkbenchShell } from "./workbench/WorkbenchShell";
import "./styles.css";

function parsePopoutConfig(): {
  panel: PanelName;
  timeframe: string;
  instrumentId: string;
  paneId: string;
} | null {
  const params = new URLSearchParams(window.location.search);
  if (params.get("popout") !== "1") return null;
  const panel = (params.get("panel") as PanelName | null) ?? "chart";
  return {
    panel,
    timeframe: params.get("timeframe") ?? "1m",
    instrumentId: params.get("instrument") ?? DEFAULT_INSTRUMENT,
    paneId: params.get("pane") ?? `popout-${Date.now()}`,
  };
}

function StandaloneDeskApp() {
  const [theme, setTheme] = useState(resolveInitialTheme);
  const popout = parsePopoutConfig();
  return (
    <WorkbenchShell
      initialTheme={theme}
      onThemeChange={setTheme}
      popoutConfig={popout}
    />
  );
}

if (window.location.pathname === LINK_PATH) {
  connectEmbeddedRelay();
} else {
  const searchParams = new URLSearchParams(window.location.search);
  const langParam = searchParams.get("lang");
  setLocale(langParam || navigator.language || "zh");
  document.documentElement.lang = getLocale() === "zh" ? "zh-CN" : "en";

  const panel = panelFromPath(window.location.pathname);
  document.title = panel ? `Talaria · ${panel}` : "Talaria Workbench";
  applyTheme(resolveInitialTheme());

  const container = document.getElementById("root");
  if (!container) {
    throw new Error("Missing #root container");
  }

  const root = createRoot(container);
  root.render(
    <div className="wb-boot-screen" role="status">
      <p>{t("正在连接 Talaria 纯数据引擎…", "Connecting to Talaria data engine…")}</p>
    </div>,
  );

  void initializeStandaloneApi()
    .then(() => {
      root.render(<StandaloneDeskApp />);
    })
    .catch((reason: unknown) => {
      root.render(
        <div className="wb-boot-screen wb-boot-error" role="alert">
          <p>
            {reason instanceof Error
              ? reason.message
              : t("无法连接 Talaria 服务", "Unable to connect to Talaria service")}
          </p>
          <button
            type="button"
            className="primary-button"
            onClick={() => window.location.reload()}
          >
            {t("重试", "Retry")}
          </button>
        </div>,
      );
    });
}
