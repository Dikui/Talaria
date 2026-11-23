import { useState } from "react";
import { createRoot } from "react-dom/client";

import { initializeStandaloneApi } from "./lib/api";
import { getLocale, setLocale, t } from "./lib/i18n";
import { applyTheme, resolveInitialTheme, THEME_STORAGE_KEY } from "./lib/theme";
import { connectEmbeddedRelay } from "./workbench/transport";
import { DESK_PATH, LINK_PATH, PANELS, panelLabel, panelFromPath, panelPath } from "./workbench/panels";
import { deskHref, standaloneLocale } from "./workbench/standaloneLocale";
import { WorkbenchPanel } from "./workbench/WorkbenchPanel";
import "./styles.css";

function DeskPages() {
  const [theme, setTheme] = useState(resolveInitialTheme);
  const panel = panelFromPath(window.location.pathname);
  const search = window.location.search;

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Use the chosen theme for this page even when storage is unavailable.
    }
  }

  return (
    <div className="talaria-desk talaria-desk--standalone desk-pages" data-theme={theme}>
      <div className="desk-navigation">
        <nav aria-label={t("Talaria 工作台", "Talaria Workbench")}>
          <a href={deskHref(DESK_PATH, search)} aria-current={panel === undefined ? "page" : undefined}>{t("工作台", "Workbench")}</a>
          {PANELS.map((item) => (
            <a key={item} href={deskHref(panelPath(item), search)} aria-current={item === panel ? "page" : undefined}>
              {panelLabel(item)}
            </a>
          ))}
        </nav>
        <button type="button" className="ghost-button" onClick={toggleTheme}
          aria-label={theme === "dark" ? t("切换为浅色主题", "Switch to light theme") : t("切换为深色主题", "Switch to dark theme")}>
          {theme === "dark" ? t("浅色", "Light") : t("深色", "Dark")}
        </button>
      </div>
      <WorkbenchPanel panel={panel ?? "chart"} persistenceKey={panel ?? "desk"} theme={theme} fullWorkbench={panel === undefined} />
    </div>
  );
}

if (window.location.pathname === LINK_PATH) {
  connectEmbeddedRelay();
} else {
  setLocale(standaloneLocale(window.location.search, navigator.language));
  document.documentElement.lang = getLocale() === "zh" ? "zh-CN" : "en";
  const panel = panelFromPath(window.location.pathname);
  document.title = `Talaria · ${panel ? panelLabel(panel) : t("工作台", "Workbench")}`;
  applyTheme(resolveInitialTheme());
  const container = document.getElementById("root");
  if (!container) throw new Error("Missing #root container");

  const root = createRoot(container);
  root.render(<p role="status">{t("正在连接 Talaria 常驻服务…", "Connecting to the Talaria resident service…")}</p>);
  void initializeStandaloneApi().then(() => root.render(<DeskPages />)).catch((reason: unknown) => {
    root.render(
      <div className="talaria-desk talaria-desk--standalone desk-connect">
        <p role="alert">{reason instanceof Error ? reason.message : t("无法连接 Talaria 常驻服务。", "Unable to connect to the Talaria resident service.")}</p>
        <button className="ghost-button" type="button" onClick={() => window.location.reload()}>{t("重试", "Retry")}</button>
      </div>,
    );
  });
}
