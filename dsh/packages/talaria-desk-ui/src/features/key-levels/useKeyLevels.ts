import { useCallback, useEffect, useState } from "react";

import type { DrawingRecord, KeyLevel, KeyLevelSet } from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { t } from "../../lib/i18n";

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
  adoptedIds: Set<string>;
  loading: boolean;
  error: string | null;
  ignoringId: string | null;
  adoptingId: string | null;
  ignore: (levelId: string) => Promise<void>;
  adopt: (level: KeyLevel) => Promise<DrawingRecord | null>;
  clearError: () => void;
}

export interface UseKeyLevelsOptions {
  snapshotId: string | null;
  instrumentId: string;
  timeframe: string;
  streamed: KeyLevelSet | null;
}

type LevelScope = Pick<UseKeyLevelsOptions, "instrumentId" | "timeframe">;

function matchesScope(levelSet: KeyLevelSet | null, scope: LevelScope): levelSet is KeyLevelSet {
  return levelSet?.instrument_id === scope.instrumentId && levelSet.timeframe === scope.timeframe;
}

export function selectKeyLevelSet(
  current: KeyLevelSet | null,
  incoming: KeyLevelSet,
  scope: LevelScope,
  source: "rest" | "stream",
): KeyLevelSet | null {
  if (!matchesScope(incoming, scope)) {
    return current;
  }
  if (!matchesScope(current, scope) || incoming.cutoff_ts > current.cutoff_ts) {
    return incoming;
  }
  return source === "stream" && incoming.cutoff_ts === current.cutoff_ts ? incoming : current;
}

function describe(caught: unknown): string {
  if (caught instanceof ApiError) {
    return caught.message;
  }
  if (caught instanceof Error) {
    return caught.message;
  }
  return t("关键位读取失败", "Failed to load key levels");
}

export function useKeyLevels({
  snapshotId,
  instrumentId,
  timeframe,
  streamed,
}: UseKeyLevelsOptions): KeyLevelsController {
  const [received, setReceived] = useState<KeyLevelSet | null>(null);
  const levelSet = matchesScope(received, { instrumentId, timeframe }) ? received : null;
  const [ignoredIds, setIgnoredIds] = useState<Set<string>>(() => new Set());
  const [adoptedIds, setAdoptedIds] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ignoringId, setIgnoringId] = useState<string | null>(null);
  const [adoptingId, setAdoptingId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(snapshotId !== null);
    setError(null);
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
          setReceived((current) =>
            selectKeyLevelSet(current, bundle, { instrumentId, timeframe }, "rest"),
          );
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
  }, [snapshotId, instrumentId, timeframe]);

  useEffect(() => {
    let cancelled = false;
    api
      .keyLevelFeedback(instrumentId)
      .then((summary) => {
        if (cancelled) {
          return;
        }
        setIgnoredIds(new Set(summary.ignored_level_ids ?? []));
        setAdoptedIds(new Set(summary.adopted_level_ids ?? []));
      })
      .catch(() => {
        if (!cancelled) {
          setIgnoredIds(new Set());
          setAdoptedIds(new Set());
        }
      });
    return () => {
      cancelled = true;
    };
  }, [instrumentId]);

  useEffect(() => {
    if (streamed !== null) {
      setReceived((current) =>
        selectKeyLevelSet(current, streamed, { instrumentId, timeframe }, "stream"),
      );
    }
  }, [streamed, instrumentId, timeframe]);

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

  const adopt = useCallback(
    async (level: KeyLevel) => {
      if (levelSet === null) {
        return null;
      }
      setAdoptingId(level.level_id);
      setError(null);
      try {
        const drawing = await api.adoptKeyLevel(
          level.level_id,
          levelSet.snapshot_id,
          timeframe,
        );
        setAdoptedIds((current) => new Set(current).add(level.level_id));
        return drawing;
      } catch (caught) {
        setError(describe(caught));
        return null;
      } finally {
        setAdoptingId(null);
      }
    },
    [levelSet, timeframe],
  );

  const clearError = useCallback(() => setError(null), []);

  return {
    levelSet,
    levels: levelSet?.levels ?? [],
    notes: levelSet?.notes ?? [],
    ignoredIds,
    adoptedIds,
    loading,
    error,
    ignoringId,
    adoptingId,
    ignore,
    adopt,
    clearError,
  };
}
