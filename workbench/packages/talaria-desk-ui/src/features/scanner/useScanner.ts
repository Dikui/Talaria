import { useCallback, useEffect, useState } from "react";
import type { ScannerFocusItem, ScannerTrigger } from "@talaria/contracts";

import { api } from "../../lib/api";
import { mergeTrigger, sortTriggers } from "./model";

export interface ScannerController {
  triggers: ScannerTrigger[];
  focus: ScannerFocusItem[];
  loading: boolean;
  scanning: boolean;
  error: string | null;
  unread: number;
  refresh: () => Promise<void>;
  scanNow: () => Promise<void>;
  ingest: (trigger: ScannerTrigger) => void;
  markSeen: () => void;
}

export function useScanner(enabled = true): ScannerController {
  const [triggers, setTriggers] = useState<ScannerTrigger[]>([]);
  const [focus, setFocus] = useState<ScannerFocusItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unread, setUnread] = useState(0);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    try {
      const [trigRes, focusRes] = await Promise.all([
        api.scannerTriggers({ limit: 50 }),
        api.scannerFocus(),
      ]);
      setTriggers(sortTriggers(Array.isArray(trigRes.triggers) ? trigRes.triggers : []));
      setFocus(Array.isArray(focusRes.items) ? focusRes.items : []);
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load scanner");
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const scanNow = useCallback(async () => {
    setScanning(true);
    try {
      const res = await api.scanNow();
      setTriggers((prev) => {
        let next = prev;
        for (const item of res.triggers) {
          next = mergeTrigger(next, item);
        }
        return sortTriggers(next);
      });
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Scan failed");
    } finally {
      setScanning(false);
    }
  }, []);

  const ingest = useCallback((trigger: ScannerTrigger) => {
    setTriggers((prev) => sortTriggers(mergeTrigger(prev, trigger)));
    setUnread((n) => n + 1);
  }, []);

  const markSeen = useCallback(() => setUnread(0), []);

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
  };
}
