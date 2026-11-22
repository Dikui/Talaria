import { useEffect, useState } from "react";
import type { KeyLevel } from "@talaria/contracts";
import { api } from "../../lib/api";
import { useReadOnlyViewport } from "../drawings/useReadOnlyViewport";
import { useMarketStream } from "../feed/useMarketStream";
import { KeyLevelPanel } from "./KeyLevelPanel";
import { defaultKeyLevelFilter, visibleKeyLevels } from "./model";
import { useKeyLevels } from "./useKeyLevels";

interface KeyLevelWorkbenchProps {
  instrumentId: string;
  timeframe: string;
  precision: number;
  cutoff: number | null;
  onAttachAlert: (level: KeyLevel) => void;
}

export function KeyLevelWorkbench({
  instrumentId, timeframe, precision, cutoff, onAttachAlert,
}: KeyLevelWorkbenchProps) {
  const [snapshotId, setSnapshotId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [filter, setFilter] = useState(defaultKeyLevelFilter);
  const readOnly = useReadOnlyViewport();
  const stream = useMarketStream(instrumentId, timeframe);
  const levels = useKeyLevels({ snapshotId, instrumentId, timeframe, streamed: stream.levels });

  useEffect(() => {
    let cancelled = false;
    api.createSnapshot(instrumentId, [timeframe], cutoff).then((snapshot) => {
      if (!cancelled) setSnapshotId(snapshot.snapshot_id);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "无法创建关键位快照");
    });
    return () => { cancelled = true; };
  }, [instrumentId, timeframe, cutoff, attempt]);

  if (error) return (
    <div>
      <p role="alert">{error}</p>
      <button type="button" className="ghost-button" onClick={() => {
        setError(null);
        setAttempt((count) => count + 1);
      }}>重试</button>
    </div>
  );

  return (
    <KeyLevelPanel
      levels={visibleKeyLevels(levels.levels, filter, levels.ignoredIds)}
      allLevels={levels.levels.filter((level) => !levels.ignoredIds.has(level.level_id))}
      filter={filter}
      onFilterChange={setFilter}
      precision={precision}
      ignoredCount={levels.ignoredIds.size}
      onIgnore={levels.ignore}
      ignoringId={levels.ignoringId}
      adoptedIds={levels.adoptedIds}
      adoptingId={levels.adoptingId}
      onAdopt={(level) => { void levels.adopt(level); }}
      onAttachAlert={onAttachAlert}
      loading={snapshotId === null || levels.loading}
      error={levels.error}
      readOnly={readOnly}
      notes={levels.notes}
    />
  );
}
