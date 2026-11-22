import { useCallback, useEffect, useRef, useState } from "react";
import type { ScannerFocusResponse, ScannerTrigger } from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { mergeTrigger, sortTriggers } from "./model";

const POLL_MS = 4_000;
const HISTORY_LIMIT = 50;

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "扫描状态读取失败";
}

export interface ScannerController {
  triggers: ScannerTrigger[];
  focus: ScannerFocusResponse | null;
  loading: boolean;
  scanning: boolean;
  error: string | null;
  unread: number;
  refresh: () => Promise<void>;
  scanNow: () => Promise<void>;
  ingest: (trigger: ScannerTrigger) => void;
  markSeen: () => void;
  clearError: () => void;
}

/**
 * 扫描结果走双通道：WS 命中即时插入，4 秒轮询补齐历史并兜住断线；
 * 两者按 trigger_id 去重，因此切标的导致 WS 重置也不会丢卡片。
 */
export function useScanner(enabled = true): ScannerController {
  const [triggers, setTriggers] = useState<ScannerTrigger[]>([]);
  const [focus, setFocus] = useState<ScannerFocusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unread, setUnread] = useState(0);
  const mounted = useRef(true);
  const seen = useRef<Set<string>>(new Set());
  const ready = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [list, nextFocus] = await Promise.all([
        api.scannerTriggers({ limit: HISTORY_LIMIT }),
        api.scannerFocus(),
      ]);
      if (!mounted.current) {
        return;
      }
      let fresh = 0;
      for (const trigger of list.triggers) {
        if (seen.current.has(trigger.trigger_id)) {
          continue;
        }
        seen.current.add(trigger.trigger_id);
        if (ready.current) {
          fresh += 1;
        }
      }
      if (!ready.current) {
        ready.current = true;
      }
      setTriggers(sortTriggers(list.triggers).slice(0, HISTORY_LIMIT));
      setFocus(nextFocus);
      setError(null);
      setLoading(false);
      if (fresh > 0) {
        setUnread((current) => current + fresh);
      }
    } catch (caught) {
      if (mounted.current) {
        setError(describe(caught));
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, [enabled, refresh]);

  const ingest = useCallback((trigger: ScannerTrigger) => {
    setTriggers((current) => {
      if (current.some((item) => item.trigger_id === trigger.trigger_id)) {
        return current;
      }
      return mergeTrigger(current, trigger);
    });
    if (seen.current.has(trigger.trigger_id)) {
      return;
    }
    seen.current.add(trigger.trigger_id);
    if (ready.current) {
      setUnread((count) => count + 1);
    }
  }, []);

  const scanNow = useCallback(async () => {
    setScanning(true);
    setError(null);
    try {
      await api.scanNow();
      await refresh();
    } catch (caught) {
      if (mounted.current) {
        setError(describe(caught));
      }
    } finally {
      if (mounted.current) {
        setScanning(false);
      }
    }
  }, [refresh]);

  const markSeen = useCallback(() => setUnread(0), []);
  const clearError = useCallback(() => setError(null), []);

  return {
    triggers,
    focus,
    loading,
    scanning,
    error,
    unread,
    refresh,
    scanNow,
    ingest,
    markSeen,
    clearError,
  };
}
