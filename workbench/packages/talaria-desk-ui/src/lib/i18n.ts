import { useSyncExternalStore } from "react";

export type Locale = "zh" | "en";

let current: Locale = "zh";
const listeners = new Set<() => void>();

export function normalizeLocale(raw?: string | null): Locale {
  return /^zh(?:[-_]|$)/i.test(raw?.trim() ?? "") ? "zh" : "en";
}

export function getLocale(): Locale {
  return current;
}

export function setLocale(raw: string): void {
  const next = normalizeLocale(raw);
  if (next === current) return;
  current = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useLocale(): Locale {
  return useSyncExternalStore(subscribe, getLocale, getLocale);
}

export function tFor(locale: Locale, zh: string, en: string): string {
  return locale === "zh" ? zh : en;
}

export function t(zh: string, en: string): string {
  return tFor(current, zh, en);
}
