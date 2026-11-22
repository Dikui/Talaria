import { useEffect } from "react";
import type { LocateTarget } from "../../workbench/link";
import { useReadOnlyViewport } from "../drawings/useReadOnlyViewport";
import { useMarketStream } from "../feed/useMarketStream";
import { ScannerPanel } from "./ScannerPanel";
import { useScanner } from "./useScanner";

interface ScannerWorkbenchProps {
  instrumentId: string;
  timeframe: string;
  onLocate: (target: LocateTarget) => void;
}

export function ScannerWorkbench({ instrumentId, timeframe, onLocate }: ScannerWorkbenchProps) {
  const controller = useScanner();
  const { scanner } = useMarketStream(instrumentId, timeframe);
  const readOnly = useReadOnlyViewport();
  useEffect(() => {
    if (scanner) controller.ingest(scanner);
  }, [scanner, controller.ingest]);

  return <ScannerPanel controller={controller} open onLocate={onLocate} readOnly={readOnly} />;
}
