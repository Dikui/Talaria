export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "talaria-theme";

function prefersDark(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

export function resolveInitialTheme(): Theme {
  if (typeof localStorage !== "undefined") {
    try {
      const stored = localStorage.getItem(THEME_STORAGE_KEY);
      if (stored === "light" || stored === "dark") {
        return stored;
      }
    } catch {
      // Ignore storage access failures.
    }
  }
  return prefersDark() ? "dark" : "light";
}

export function applyTheme(
  theme: Theme,
  target: HTMLElement = document.documentElement,
): void {
  target.dataset.theme = theme;
  target.style.colorScheme = theme;
}
