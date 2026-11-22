import { useEffect, useState } from "react";
import type { DrawingRecord, InstrumentSpec } from "@talaria/contracts";
import { api } from "../../lib/api";
import type { LocateTarget } from "../../workbench/link";
import { useMarketStream } from "../feed/useMarketStream";
import { AlertPanel, AlertToast, type AlertFormSeed } from "./AlertPanel";
import { useAlerts } from "./useAlerts";

interface AlertWorkbenchProps {
  instrumentId: string;
  timeframe: string;
  instruments: InstrumentSpec[];
  onLocate: (target: LocateTarget) => void;
  formSeed?: AlertFormSeed;
  onClose?: () => void;
}

export function AlertWorkbench({
  instrumentId, timeframe, instruments, onLocate, formSeed, onClose,
}: AlertWorkbenchProps) {
  const controller = useAlerts();
  const { alert } = useMarketStream(instrumentId, timeframe);
  const [drawings, setDrawings] = useState<DrawingRecord[]>([]);
  const [drawingError, setDrawingError] = useState(false);

  useEffect(() => {
    if (alert) controller.ingestTrigger(alert);
  }, [alert, controller.ingestTrigger]);

  useEffect(() => {
    let cancelled = false;
    setDrawings([]);
    setDrawingError(false);
    api.drawings(instrumentId, timeframe).then((response) => {
      if (!cancelled) setDrawings(response.drawings);
    }).catch(() => {
      if (!cancelled) setDrawingError(true);
    });
    return () => { cancelled = true; };
  }, [instrumentId, timeframe]);

  return (
    <>
      {drawingError && <p role="status">无法读取可用于预警的画线。</p>}
      <AlertPanel
        controller={controller}
        instruments={instruments}
        drawings={drawings}
        currentInstrument={instrumentId}
        currentTimeframe={timeframe}
        formSeed={formSeed ?? null}
        onLocate={onLocate}
        onClose={onClose}
      />
      {controller.toast && (
        <AlertToast
          trigger={controller.toast}
          onLocate={(trigger) => {
            onLocate(trigger);
            controller.clearToast();
          }}
          onDismiss={controller.clearToast}
        />
      )}
    </>
  );
}
