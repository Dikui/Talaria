import { useEffect, useState } from "react";
import type { DrawingRecord, InstrumentSpec } from "@talaria/contracts";
import { api } from "../../lib/api";
import type { Theme } from "../../lib/theme";
import type { LocateTarget, PanelLink } from "../../workbench/link";
import { useMarketStream } from "../feed/useMarketStream";
import { AlertPanel, AlertToast, type AlertFormSeed } from "./AlertPanel";
import { useAlerts } from "./useAlerts";

interface AlertWorkbenchProps {
  instrumentId: string;
  timeframe: string;
  instruments: InstrumentSpec[];
  theme: Theme;
  onLocate: (target: LocateTarget) => void;
  link: Pick<PanelLink, "subscribeDrawings">;
  formSeed?: AlertFormSeed;
  onClose?: () => void;
}

export function AlertWorkbench({
  instrumentId, timeframe, instruments, theme, onLocate, link, formSeed, onClose,
}: AlertWorkbenchProps) {
  const controller = useAlerts();
  const { alert, deniedAlerts } = useMarketStream(instrumentId, timeframe);
  const [drawings, setDrawings] = useState<DrawingRecord[]>([]);
  const [drawingError, setDrawingError] = useState(false);

  useEffect(() => {
    if (alert) controller.ingestTrigger(alert);
  }, [alert, controller.ingestTrigger]);

  useEffect(() => {
    let request = 0;
    setDrawings([]);
    setDrawingError(false);
    function refreshDrawings() {
      const current = ++request;
      api.drawings(instrumentId, timeframe).then((response) => {
        if (current !== request) return;
        setDrawings(response.drawings);
        setDrawingError(false);
      }).catch(() => {
        if (current === request) setDrawingError(true);
      });
    }
    const stop = link.subscribeDrawings(instrumentId, refreshDrawings);
    refreshDrawings();
    return () => {
      request += 1;
      stop();
    };
  }, [instrumentId, timeframe, link]);

  return (
    <>
      {drawingError && <p role="status">无法读取可用于预警的画线。</p>}
      <AlertPanel
        controller={controller}
        deniedAlerts={deniedAlerts}
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
          theme={theme}
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
