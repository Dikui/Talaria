import { useCallback, useEffect, useState } from "react";
import type {
  BriefingConsultResponse,
  ScannerBriefing,
} from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { t } from "../../lib/i18n";

const POLL_MS = 5000;
const LIST_LIMIT = 50;

export interface BriefingsController {
  briefings: ScannerBriefing[];
  unread: number;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  markRead: (briefingId: string) => Promise<void>;
  consult: (
    briefingId: string,
    question?: string,
  ) => Promise<BriefingConsultResponse | null>;
  clearError: () => void;
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return t("读取简报失败", "Failed to load briefings");
}

export function useBriefings(): BriefingsController {
  const [briefings, setBriefings] = useState<ScannerBriefing[]>([]);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await api.briefings({ limit: LIST_LIMIT });
      setBriefings(response.briefings ?? []);
      setUnread(response.unread ?? 0);
      setError(null);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const markRead = useCallback(async (briefingId: string) => {
    try {
      const updated = await api.markBriefingRead(briefingId);
      setBriefings((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setUnread((current) => Math.max(0, current - 1));
    } catch (reason) {
      setError(describeError(reason));
    }
  }, []);

  const consult = useCallback(
    async (briefingId: string, question?: string) => {
      try {
        const seed = await api.briefingConsult(briefingId, question);
        setError(null);
        return seed;
      } catch (reason) {
        setError(describeError(reason));
        return null;
      }
    },
    [],
  );

  const clearError = useCallback(() => setError(null), []);

  return {
    briefings,
    unread,
    loading,
    error,
    refresh,
    markRead,
    consult,
    clearError,
  };
}
