import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ConsultationRecord,
  ConsultRequest,
  UsageSummaryResponse,
} from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { isPending } from "./model";

function describe(caught: unknown): string {
  if (caught instanceof ApiError) return caught.message;
  if (caught instanceof Error) return caught.message;
  return "协同会诊暂不可用";
}

/** 面板挂载时拉最近会诊；有 PENDING 时快轮询，全部落定后降为低频同步。 */
export function useConsultation() {
  const [records, setRecords] = useState<ConsultationRecord[]>([]);
  const [usage, setUsage] = useState<UsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const list = await api.consultations(30);
      if (!mounted.current) return;
      setRecords(list.consultations ?? []);
      setError(null);
    } catch (caught) {
      if (mounted.current) setError(describe(caught));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, []);

  const refreshUsage = useCallback(async () => {
    try {
      const value = await api.collaborationUsage(100);
      if (mounted.current) setUsage(value);
    } catch {
      if (mounted.current) setUsage(null);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    void refreshUsage();
    return () => {
      mounted.current = false;
    };
  }, [refresh, refreshUsage]);

  const pending = records.some(isPending);
  useEffect(() => {
    if (!mounted.current) return;
    const interval = window.setInterval(
      () => {
        void refresh();
        if (!pending) void refreshUsage();
      },
      pending ? 800 : 5000,
    );
    return () => window.clearInterval(interval);
  }, [pending, refresh, refreshUsage]);

  const start = useCallback(
    async (payload: ConsultRequest): Promise<ConsultationRecord> => {
      setSubmitting(true);
      setError(null);
      try {
        const response = await api.consult(payload);
        const record = await api.consultation(response.consultation_id);
        if (mounted.current) {
          setRecords((previous) => [
            record,
            ...previous.filter(
              (item) => item.consultation_id !== record.consultation_id,
            ),
          ]);
        }
        return record;
      } catch (caught) {
        if (mounted.current) setError(describe(caught));
        throw caught;
      } finally {
        if (mounted.current) setSubmitting(false);
      }
    },
    [],
  );

  const clearError = useCallback(() => setError(null), []);

  return {
    records,
    usage: usage?.summary ?? null,
    loading,
    submitting,
    error,
    pending,
    start,
    refresh,
    refreshUsage,
    clearError,
  };
}
