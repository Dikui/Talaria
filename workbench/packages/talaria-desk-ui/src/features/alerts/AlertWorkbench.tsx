import { useEffect, useState } from "react";
import type { DrawingRecord, InstrumentSpec } from "@talaria/contracts";

import { api } from "../../lib/api";
import type { LocateTarget, PanelLink } from "../../workbench/link";
import { AlertPanel, type AlertFormSeed } from "./AlertPanel";
import type { AlertsController } from "./useAlerts";

export interface AlertWorkbenchProps {
  controller: AlertsController;
  deniedAlerts?: string[];
  instrumentId: string;
  timeframe: string;
  instruments: InstrumentSpec[];
  initialDrawings?: DrawingRecord[];
  onLocate: (target: LocateTarget) => void;
  link: Pick<PanelLink, "subscribeDrawings">;
  formSeed?: AlertFormSeed | null;
  onSeedConsumed?: (nonce: number) => void;
  replayActive?: boolean;
}

export function AlertWorkbench({
  controller,
  deniedAlerts = [],
  instrumentId,
  timeframe,
  instruments,
  initialDrawings,
  onLocate,
  link,
  formSeed,
  onSeedConsumed,
  replayActive = false,
}: AlertWorkbenchProps) {
  const [drawings, setDrawings] = useState<DrawingRecord[]>(() => initialDrawings ?? []);

  useEffect(() => {
    if (initialDrawings && initialDrawings.length > 0 && drawings.length === 0) {
      setDrawings(initialDrawings);
    }
  }, [initialDrawings, drawings.length]);

  useEffect(() => {
    let active = true;
    const refreshDrawings = () => {
      api
        .drawings(instrumentId, timeframe, { all_timeframes: true })
        .then((res) => {
          if (active) setDrawings(res.drawings);
        })
        .catch(() => {
          if (active) setDrawings([]);
        });
    };
    const stop = link.subscribeDrawings(instrumentId, refreshDrawings);
    refreshDrawings();
    return () => {
      active = false;
      stop();
    };
  }, [instrumentId, timeframe, link]);

  return (
    <AlertPanel
      controller={controller}
      deniedAlerts={deniedAlerts}
      instruments={instruments}
      drawings={drawings}
      currentInstrument={instrumentId}
      currentTimeframe={timeframe}
      formSeed={formSeed ?? null}
      onSeedConsumed={onSeedConsumed}
      replayActive={replayActive}
      onLocate={onLocate}
    />
  );
}
