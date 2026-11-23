import { normalizeLocale, type Locale } from "../lib/i18n";

function override(search: string): Locale | undefined {
  const lang = new URLSearchParams(search).get("lang");
  return lang && /^(?:en|zh)(?:[-_][\w-]+)?$/i.test(lang) ? normalizeLocale(lang) : undefined;
}

export function standaloneLocale(search: string, browserLanguage: string): Locale {
  return override(search) ?? normalizeLocale(browserLanguage);
}

/** Carry only the language override, not arbitrary query parameters, across pages. */
export function deskHref(path: string, search: string): string {
  const locale = override(search);
  return locale ? `${path}?lang=${locale}` : path;
}
