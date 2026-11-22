import { useEffect, useState, type ReactNode } from "react";
import type { InstrumentSpec, KeyLevel } from "@talaria/contracts";
import { ChartWorkbench } from "../App";
import { AlertWorkbench } from "../features/alerts/AlertWorkbench";
import type { AlertFormSeed } from "../features/alerts/AlertPanel";
import { KeyLevelWorkbench } from "../features/key-levels/KeyLevelWorkbench";
import { ScannerWorkbench } from "../features/scanner/ScannerWorkbench";
import { api } from "../lib/api";
import type { Theme } from "../lib/theme";
import type { LinkTransport, PanelLink } from "./link";
import { LinkControls, SymbolControls } from "./PanelControls";
import { PANEL_LABELS, type PanelName } from "./panels";
import { useLinkSnapshot, usePanelLink } from "./usePanelLink";

interface WorkbenchPanelProps {
  panel: PanelName;
  persistenceKey: string;
  theme: Theme;
  embedded?: boolean;
  fullWorkbench?: boolean;
  transport?: LinkTransport;
  tools?: ReactNode;
}

export function WorkbenchPanel({
  panel, persistenceKey, theme, embedded = false, fullWorkbench = false, transport, tools,
}: WorkbenchPanelProps) {
  const link = usePanelLink(panel, persistenceKey, transport);
  if (link === null) return <p role="status">正在打开{PANEL_LABELS[panel]}…</p>;
  if (panel === "chart") {
    return <ChartWorkbench embedded={embedded} link={link} theme={theme} fullWorkbench={fullWorkbench} tools={tools} />;
  }
  return <FeatureWorkbench panel={panel} link={link} theme={theme} tools={tools} />;
}

function FeatureWorkbench({
  panel, link, theme, tools,
}: { panel: Exclude<PanelName, "chart">; link: PanelLink; theme: Theme; tools?: ReactNode }) {
  const { instrumentId, timeframe, locateTs } = useLinkSnapshot(link);
  const [instruments, setInstruments] = useState<InstrumentSpec[]>([]);
  const [instrumentError, setInstrumentError] = useState(false);
  const [alertSeed, setAlertSeed] = useState<AlertFormSeed>();
  const precision = instruments.find((instrument) => instrument.instrument_id === instrumentId)?.price_precision ?? 2;

  useEffect(() => {
    let cancelled = false;
    api.instruments().then((rows) => {
      if (!cancelled) setInstruments(rows);
    }).catch(() => {
      if (!cancelled) setInstrumentError(true);
    });
    return () => { cancelled = true; };
  }, []);

  function attachAlert(level: KeyLevel) {
    setAlertSeed({
      nonce: Date.now(),
      instrumentId,
      timeframe,
      levelId: level.level_id,
      levelKind: level.kind,
    });
  }

  return (
    <section className="talaria-desk feature-workbench" data-theme={theme} aria-label={PANEL_LABELS[panel]}>
      <header className="feature-workbench-header">
        <div className="feature-workbench-title">
          <h1>Talaria {PANEL_LABELS[panel]}</h1>
          <LinkControls link={link} />
          {tools}
        </div>
        <div className="feature-workbench-symbol">
          <SymbolControls link={link} instruments={instruments} />
          {instrumentError && <span role="status">无法读取标的列表。</span>}
        </div>
      </header>
      <div className="feature-workbench-body">
        {panel === "alerts" && (
          <AlertWorkbench
            instrumentId={instrumentId}
            timeframe={timeframe}
            instruments={instruments}
            onLocate={link.locate}
          />
        )}
        {panel === "scanner" && (
          <ScannerWorkbench instrumentId={instrumentId} timeframe={timeframe} onLocate={link.locate} />
        )}
        {panel === "levels" && (
          <KeyLevelWorkbench
            key={JSON.stringify([instrumentId, timeframe, locateTs])}
            instrumentId={instrumentId}
            timeframe={timeframe}
            precision={precision}
            cutoff={locateTs}
            onAttachAlert={attachAlert}
          />
        )}
        {alertSeed && (
          <AlertWorkbench
            instrumentId={alertSeed.instrumentId ?? instrumentId}
            timeframe={alertSeed.timeframe ?? timeframe}
            instruments={instruments}
            onLocate={link.locate}
            formSeed={alertSeed}
            onClose={() => setAlertSeed(undefined)}
          />
        )}
      </div>
    </section>
  );
}
