import { useCallback, useEffect, useState } from "react";

export const LAST_SUB_ACCOUNT_STORAGE_KEY = "talaria-last-sub-account";
const LAST_SUB_ACCOUNT_EVENT = "talaria:last-sub-account";

let memoryFallbackSubAccount = "";

function resolveStorage(): Storage | null {
  try {
    const candidate =
      typeof window !== "undefined" && window.localStorage
        ? window.localStorage
        : typeof globalThis !== "undefined"
          ? (globalThis as { localStorage?: Storage }).localStorage
          : undefined;
    if (
      candidate &&
      typeof candidate.getItem === "function" &&
      typeof candidate.setItem === "function"
    ) {
      return candidate;
    }
  } catch {
    // Ignore storage access errors in restricted environments.
  }
  return null;
}

export function getLastSubAccount(): string {
  const storage = resolveStorage();
  if (storage) {
    try {
      return (storage.getItem(LAST_SUB_ACCOUNT_STORAGE_KEY) ?? "").trim();
    } catch {
      return memoryFallbackSubAccount;
    }
  }
  return memoryFallbackSubAccount;
}

export function setLastSubAccount(subAccountId: string): void {
  const trimmed = subAccountId.trim();
  if (!trimmed) return;
  memoryFallbackSubAccount = trimmed;
  const storage = resolveStorage();
  if (storage) {
    try {
      storage.setItem(LAST_SUB_ACCOUNT_STORAGE_KEY, trimmed);
    } catch {
      // Ignore storage errors in restricted environments.
    }
  }
  try {
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent(LAST_SUB_ACCOUNT_EVENT, { detail: trimmed }));
    }
  } catch {
    // Ignore event dispatch errors.
  }
}

export function clearLastSubAccount(): void {
  memoryFallbackSubAccount = "";
  const storage = resolveStorage();
  if (storage && typeof storage.removeItem === "function") {
    try {
      storage.removeItem(LAST_SUB_ACCOUNT_STORAGE_KEY);
    } catch {
      // Ignore storage errors.
    }
  }
}

/**
 * Resolves the default sub-account for a new draft or sizing preview (spec D8):
 * explicit seed -> remembered last-used sub-account -> first available running sub-account -> "".
 */
export function resolveDefaultSubAccount(
  availableAccounts: readonly string[],
  explicit?: string | null,
): string {
  const explicitTrimmed = (explicit ?? "").trim();
  if (explicitTrimmed) return explicitTrimmed;

  const remembered = getLastSubAccount();
  if (remembered) {
    if (availableAccounts.length === 0 || availableAccounts.includes(remembered)) {
      return remembered;
    }
    // Keep remembered account even if account process is temporarily offline (user can still stage).
    return remembered;
  }

  return availableAccounts[0] ?? "";
}

export function useLastSubAccount(availableAccounts: readonly string[] = []) {
  const [lastSubAccount, setLastState] = useState<string>(() => getLastSubAccount());

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onCustom = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail;
      setLastState(typeof detail === "string" ? detail : getLastSubAccount());
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === LAST_SUB_ACCOUNT_STORAGE_KEY) {
        setLastState(getLastSubAccount());
      }
    };
    window.addEventListener(LAST_SUB_ACCOUNT_EVENT, onCustom);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(LAST_SUB_ACCOUNT_EVENT, onCustom);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const remember = useCallback((subAccountId: string) => {
    const trimmed = subAccountId.trim();
    if (!trimmed) return;
    setLastSubAccount(trimmed);
    setLastState(trimmed);
  }, []);

  const resolved = lastSubAccount || availableAccounts[0] || "";

  return {
    lastSubAccount,
    resolvedSubAccount: resolved,
    rememberSubAccount: remember,
  };
}
