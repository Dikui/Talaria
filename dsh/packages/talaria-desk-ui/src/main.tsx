import { useState } from "react";
import { createRoot } from "react-dom/client";

import { initializeStandaloneApi } from "./lib/api";
import { applyTheme, resolveInitialTheme, THEME_STORAGE_KEY } from "./lib/theme";
import { connectEmbeddedRelay } from "./workbench/transport";
import { DESK_PATH, LINK_PATH, PANELS, PANEL_LABELS, panelFromPath, panelPath } from "./workbench/panels";
import { WorkbenchPanel } from "./workbench/WorkbenchPanel";
import "./styles.css";

function DeskPages() {
  const [theme, setTheme] = useState(resolveInitialTheme);
  const panel = panelFromPath(window.location.pathname);

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
        <nav aria-label="Talaria 工作台">
          <a href={DESK_PATH} aria-current={panel === undefined ? "page" : undefined}>工作台</a>
          {PANELS.map((item) => (
            <a key={item} href={panelPath(item)} aria-current={item === panel ? "page" : undefined}>
              {PANEL_LABELS[item]}
            </a>
          ))}
        </nav>
        <button type="button" className="ghost-button" onClick={toggleTheme}
          aria-label={theme === "dark" ? "切换为浅色主题" : "切换为深色主题"}>
          {theme === "dark" ? "浅色" : "深色"}
        </button>
      </div>
      <WorkbenchPanel panel={panel ?? "chart"} persistenceKey={panel ?? "desk"} theme={theme} fullWorkbench={panel === undefined} />
    </div>
  );
}

if (window.location.pathname === LINK_PATH) {
  connectEmbeddedRelay();
} else {
  applyTheme(resolveInitialTheme());
  const container = document.getElementById("root");
  if (!container) throw new Error("缺少 #root 容器");

  const root = createRoot(container);
  root.render(<p role="status">正在连接 Talaria 常驻服务…</p>);
  void initializeStandaloneApi().then(() => root.render(<DeskPages />)).catch((reason: unknown) => {
    root.render(
      <div className="talaria-desk talaria-desk--standalone desk-connect">
        <p role="alert">{reason instanceof Error ? reason.message : "无法连接 Talaria 常驻服务。"}</p>
        <button className="ghost-button" type="button" onClick={() => window.location.reload()}>重试</button>
      </div>,
    );
  });
}
