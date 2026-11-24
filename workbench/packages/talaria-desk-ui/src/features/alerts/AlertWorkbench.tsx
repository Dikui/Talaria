import { useEffect, useState } from "react";
import type { DrawingRecord, InstrumentSpec } from "@talaria/contracts";

import { api } from "../../lib/api";
import type { LocateTarget, PanelLink } from "../../workbench/link";
import { useMarketStream } from "../feed/useMarketStream";
import { AlertPanel, AlertToast, type AlertFormSeed } from "./AlertPanel";
import { useAlerts } from "./useAlerts";

export interface AlertWorkbenchProps {
  instrumentId: string;
  timeframe: string;
  instruments: InstrumentSpec[];
  onLocate: (target: LocateTarget) => void;
  link: Pick<PanelLink, "subscribeDrawings">;
  formSeed?: AlertFormSeed | null;
}

export function AlertWorkbench({
  instrumentId,
  timeframe,
  instruments,
  onLocate,
  link,
  formSeed,
}: AlertWorkbenchProps) {
  const controller = useAlerts();
  const { alert, deniedAlerts } = useMarketStream(instrumentId, timeframe);
  const [drawings, setDrawings] = useState<DrawingRecord[]>([]);

  useEffect(() => {
    if (alert) controller.ingestTrigger(alert);
  }, [alert, controller.ingestTrigger]);

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
    <>
      <AlertPanel
        controller={controller}
        deniedAlerts={deniedAlerts}
        instruments={instruments}
        drawings={drawings}
        currentInstrument={instrumentId}
        currentTimeframe={timeframe}
        formSeed={formSeed ?? null}
        onLocate={onLocate}
      />
      {controller.toast && (
        <AlertToast
          trigger={controller.toast}
          onLocate={(trig) => {
            onLocate(trig);
            controller.clearToast();
          }}
          onDismiss={controller.clearToast}
        />
      )}
    </>
  );
}
