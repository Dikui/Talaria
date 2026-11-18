export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "talaria-theme";

function prefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

/** 默认跟随系统；用户显式切换后以 localStorage 为准。 */
export function resolveInitialTheme(): Theme {
  if (typeof localStorage !== "undefined") {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") {
      return stored;
    }
  }
  return prefersDark() ? "dark" : "light";
}

/**
 * 独立页面把主题写到 ``documentElement``；嵌入 DSH 时只写到工作台根节点，
 * 避免覆盖宿主自己的主题。
 */
export function applyTheme(
  theme: Theme,
  target: HTMLElement = document.documentElement,
): void {
  target.dataset.theme = theme;
  target.style.colorScheme = theme;
}
