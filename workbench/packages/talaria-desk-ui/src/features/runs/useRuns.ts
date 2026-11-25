import { useCallback, useEffect, useState } from "react";
import type {
  RunEquityPoint,
  RunExportSummary,
  RunRoundRecord,
} from "@talaria/contracts";

import { api } from "../../lib/api";

export interface RunsController {
  runs: RunExportSummary[];
  selectedRunId: string | null;
  selectedRun: RunExportSummary | null;
  rounds: RunRoundRecord[];
  equityPoints: RunEquityPoint[];
  selectedRoundId: string | null;
  loadingRuns: boolean;
  loadingDetail: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  selectRun: (runId: string | null) => void;
  selectRound: (roundId: string | null) => void;
}

export function useRuns(): RunsController {
  const [runs, setRuns] = useState<RunExportSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<RunExportSummary | null>(null);
  const [rounds, setRounds] = useState<RunRoundRecord[]>([]);
  const [equityPoints, setEquityPoints] = useState<RunEquityPoint[]>([]);
  const [selectedRoundId, setSelectedRoundId] = useState<string | null>(null);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoadingRuns(true);
    try {
      const res = await api.runs();
      const list = Array.isArray(res.runs) ? res.runs : [];
      setRuns(list);
      setError(null);
      setSelectedRunId((prev) => {
        if (prev && list.some((r) => r.run_id === prev)) return prev;
        return list[0]?.run_id ?? null;
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to load runs");
    } finally {
      setLoadingRuns(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null);
      setRounds([]);
      setEquityPoints([]);
      setSelectedRoundId(null);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    setSelectedRoundId(null);

    void Promise.all([
      api.run(selectedRunId),
      api.runRounds(selectedRunId),
      api.runEquity(selectedRunId),
    ])
      .then(([summary, roundsRes, equityRes]) => {
        if (cancelled) return;
        setSelectedRun(summary);
        setRounds(Array.isArray(roundsRes.rounds) ? roundsRes.rounds : []);
        setEquityPoints(Array.isArray(equityRes.points) ? equityRes.points : []);
        setError(null);
      })
      .catch((caught) => {
        if (cancelled) return;
        setError(caught instanceof Error ? caught.message : "Failed to load run details");
      })
      .finally(() => {
        if (!cancelled) setLoadingDetail(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  const selectRun = useCallback((runId: string | null) => {
    setSelectedRunId(runId);
  }, []);

  const selectRound = useCallback((roundId: string | null) => {
    setSelectedRoundId(roundId);
  }, []);

  return {
    runs,
    selectedRunId,
    selectedRun,
    rounds,
    equityPoints,
    selectedRoundId,
    loadingRuns,
    loadingDetail,
    error,
    refresh,
    selectRun,
    selectRound,
  };
}
