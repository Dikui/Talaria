import { useCallback, useEffect, useState } from "react";

import type { KeyLevel, KeyLevelSet } from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";

/**
 * 关键位只读数据源：REST 首帧 + WS `levels` 增量覆盖 + 忽略反馈。
 *
 * 忽略状态以服务端为真源，操作成功后才更新本地集合；失败保留旧值并暴露错误，
 * 避免刷新后被“看起来已忽略”的假象误导。
 */
export interface KeyLevelsController {
  levelSet: KeyLevelSet | null;
  levels: KeyLevel[];
  notes: string[];
  ignoredIds: Set<string>;
  loading: boolean;
  error: string | null;
  ignoringId: string | null;
  ignore: (levelId: string) => Promise<void>;
  clearError: () => void;
}

export interface UseKeyLevelsOptions {
  snapshotId: string | null;
  instrumentId: string;
  timeframe: string;
  streamed: KeyLevelSet | null;
}

function describe(caught: unknown): string {
  if (caught instanceof ApiError) {
    return caught.message;
  }
  if (caught instanceof Error) {
    return caught.message;
  }
  return "关键位读取失败";
}

export function useKeyLevels({
  snapshotId,
  instrumentId,
  timeframe,
  streamed,
}: UseKeyLevelsOptions): KeyLevelsController {
  const [levelSet, setLevelSet] = useState<KeyLevelSet | null>(null);
  const [ignoredIds, setIgnoredIds] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ignoringId, setIgnoringId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(snapshotId !== null);
    setError(null);
    setLevelSet(null);
    if (snapshotId === null) {
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }
    api
      .keyLevels(snapshotId, timeframe, { limit: 200 })
      .then((bundle) => {
        if (!cancelled) {
          setLevelSet(bundle);
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          setError(describe(caught));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [snapshotId, timeframe]);

  useEffect(() => {
    let cancelled = false;
    api
      .ignoredKeyLevels(instrumentId)
      .then((listing) => {
        if (cancelled) {
          return;
        }
        setIgnoredIds(
          new Set(
            (listing.entries ?? [])
              .filter((entry) => entry.ignored)
              .map((entry) => entry.level_id),
          ),
        );
      })
      .catch(() => {
        if (!cancelled) {
          setIgnoredIds(new Set());
        }
      });
    return () => {
      cancelled = true;
    };
  }, [instrumentId]);

  useEffect(() => {
    if (
      streamed !== null &&
      streamed.snapshot_id === snapshotId &&
      streamed.instrument_id === instrumentId &&
      streamed.timeframe === timeframe
    ) {
      setLevelSet(streamed);
    }
  }, [streamed, snapshotId, instrumentId, timeframe]);

  const ignore = useCallback(
    async (levelId: string) => {
      setIgnoringId(levelId);
      setError(null);
      try {
        const listing = await api.setKeyLevelIgnored(instrumentId, levelId, true);
        setIgnoredIds(
          new Set(
            (listing.entries ?? [])
              .filter((entry) => entry.ignored)
              .map((entry) => entry.level_id),
          ),
        );
      } catch (caught) {
        setError(describe(caught));
      } finally {
        setIgnoringId(null);
      }
    },
    [instrumentId],
  );

  const clearError = useCallback(() => setError(null), []);

  return {
    levelSet,
    levels: levelSet?.levels ?? [],
    notes: levelSet?.notes ?? [],
    ignoredIds,
    loading,
    error,
    ignoringId,
    ignore,
    clearError,
  };
}
