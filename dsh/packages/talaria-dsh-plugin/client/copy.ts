import { useMemo } from "react";
import { useLocale } from "@talaria/desk-ui";
import { MESSAGES } from "./messages.mjs";

export type Copy = Record<keyof typeof MESSAGES, string>;

/** Build the whole visible-copy map for one locale. */
export function copyFor(locale: "zh" | "en"): Copy {
  return Object.fromEntries(
    Object.entries(MESSAGES).map(([key, value]) => [key, value[locale]]),
  ) as Copy;
}

/**
 * Re-renders every consumer when the host locale changes. desk-ui's store is
 * kept in sync by syncHostLocale() in the plugin apply hook.
 */
export function useCopy(): Copy {
  const locale = useLocale();
  return useMemo(() => copyFor(locale), [locale]);
}
