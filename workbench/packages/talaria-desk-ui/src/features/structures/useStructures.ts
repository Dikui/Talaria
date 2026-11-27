import { useCallback, useEffect, useState } from "react";
import type {
  DrawingRecord,
  PriceScaleMode,
  StructureFeature,
  StructureSet,
} from "@talaria/contracts";

import { ApiError, api } from "../../lib/api";
import { t } from "../../lib/i18n";

export interface StructuresController {
  structureSet: StructureSet | null;
  structures: StructureFeature[];
  notes: string[];
  ignoredIds: Set<string>;
  adoptedIds: Set<string>;
  loading: boolean;
  error: string | null;
  ignoringId: string | null;
  adoptingId: string | null;
  ignore: (structureId: string) => Promise<void>;
  adopt: (feature: StructureFeature) => Promise<DrawingRecord | null>;
  clearError: () => void;
}

export interface UseStructuresOptions {
  snapshotId: string | null;
  instrumentId: string;
  timeframe: string;
  scale: PriceScaleMode;
  htfTimeframes: readonly string[];
  streamed: StructureSet | null;
}

type StructureScope = Pick<UseStructuresOptions, "instrumentId" | "timeframe">;

function matchesScope(
  structureSet: StructureSet | null,
  scope: StructureScope,
): structureSet is StructureSet {
  return (
    structureSet?.instrument_id === scope.instrumentId &&
    structureSet.timeframe === scope.timeframe
  );
}

export function selectStructureSet(
  current: StructureSet | null,
  incoming: StructureSet,
  scope: StructureScope,
  source: "rest" | "stream",
): StructureSet | null {
  if (!matchesScope(incoming, scope)) return current;
  if (!matchesScope(current, scope) || incoming.cutoff_ts > current.cutoff_ts) {
    return incoming;
  }
  return source === "stream" && incoming.cutoff_ts === current.cutoff_ts
    ? incoming
    : current;
}

function describe(caught: unknown): string {
  if (caught instanceof ApiError) return caught.message;
  if (caught instanceof Error) return caught.message;
  return t("市场结构读取失败", "Failed to load market structures");
}

export function useStructures({
  snapshotId,
  instrumentId,
  timeframe,
  scale,
  htfTimeframes,
  streamed,
}: UseStructuresOptions): StructuresController {
  const [received, setReceived] = useState<StructureSet | null>(null);
  const structureSet = matchesScope(received, { instrumentId, timeframe }) ? received : null;
  const [ignoredIds, setIgnoredIds] = useState<Set<string>>(() => new Set());
  const [adoptedIds, setAdoptedIds] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ignoringId, setIgnoringId] = useState<string | null>(null);
  const [adoptingId, setAdoptingId] = useState<string | null>(null);

  const htfKey = htfTimeframes.join(",");

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
      .structures(snapshotId, timeframe, {
        scale,
        htf: htfKey ? htfKey.split(",") : undefined,
        limit: 200,
      })
      .then((bundle) => {
        if (!cancelled) {
          setReceived(bundle);
        }
      })
      .catch((caught) => {
        if (!cancelled) setError(describe(caught));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [snapshotId, instrumentId, timeframe, scale, htfKey]);

  useEffect(() => {
    let cancelled = false;
    api
      .structureFeedback(instrumentId)
      .then((summary) => {
        if (cancelled) return;
        setIgnoredIds(new Set(summary.ignored_structure_ids ?? []));
        setAdoptedIds(new Set(summary.adopted_structure_ids ?? []));
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
        selectStructureSet(current, streamed, { instrumentId, timeframe }, "stream"),
      );
    }
  }, [streamed, instrumentId, timeframe]);

  const ignore = useCallback(
    async (structureId: string) => {
      setIgnoringId(structureId);
      setError(null);
      try {
        const listing = await api.setStructureIgnored(instrumentId, structureId, true);
        setIgnoredIds(
          new Set(
            (listing.entries ?? [])
              .filter((entry) => entry.ignored)
              .map((entry) => entry.structure_id),
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
    async (feature: StructureFeature) => {
      if (structureSet === null) return null;
      setAdoptingId(feature.structure_id);
      setError(null);
      try {
        const drawing = await api.adoptStructure(
          feature.structure_id,
          structureSet.snapshot_id,
          timeframe,
        );
        setAdoptedIds((current) => new Set(current).add(feature.structure_id));
        return drawing;
      } catch (caught) {
        setError(describe(caught));
        return null;
      } finally {
        setAdoptingId(null);
      }
    },
    [structureSet, timeframe],
  );

  const clearError = useCallback(() => setError(null), []);

  return {
    structureSet,
    structures: structureSet?.structures ?? [],
    notes: structureSet?.notes ?? [],
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
