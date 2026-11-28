import type { DrawingRecord, InstrumentSpec } from "@talaria/contracts";

import { AlertWorkbench } from "../features/alerts/AlertWorkbench";
import type { AlertFormSeed } from "../features/alerts/AlertPanel";
import type { AlertsController } from "../features/alerts/useAlerts";
import { ScannerPanel } from "../features/scanner/ScannerPanel";
import type { ScannerController } from "../features/scanner/useScanner";
import { t, useLocale } from "../lib/i18n";
import type { MonitorSubTab } from "./layoutStore";
import type { LocateTarget, PanelLink } from "./link";

export interface MonitorPanelProps {
  activeTab: MonitorSubTab;
  onTabChange: (tab: MonitorSubTab) => void;
  scannerController: ScannerController;
  alertsController: AlertsController;
  deniedAlerts: string[];
  instrumentId: string;
  timeframe: string;
  instruments: InstrumentSpec[];
  drawings: DrawingRecord[];
  link: Pick<PanelLink, "subscribeDrawings">;
  alertSeed: AlertFormSeed | null;
  onSeedConsumed: (nonce: number) => void;
  onLocate: (target: LocateTarget) => void;
  readOnly: boolean;
  replayActive: boolean;
}

export function MonitorPanel({
  activeTab,
  onTabChange,
  scannerController,
  alertsController,
  deniedAlerts,
  instrumentId,
  timeframe,
  instruments,
  drawings,
  link,
  alertSeed,
  onSeedConsumed,
  onLocate,
  readOnly,
  replayActive,
}: MonitorPanelProps) {
  useLocale();
  const scannerCount = scannerController.triggers.length;
  const alertBadge =
    alertsController.unread > 0
      ? alertsController.unread
      : alertsController.alerts.length;

  return (
    <div className="wb-panel-shell" data-testid="monitor-panel">
      <div className="wb-panel-subtabs" role="tablist" aria-label={t("监控视图", "Monitor views")}>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "scanner"}
          className={`wb-subtab-btn${activeTab === "scanner" ? " is-active" : ""}`}
          data-testid="monitor-tab-scanner"
          onClick={() => onTabChange("scanner")}
        >
          <span>{t("扫描", "Scanner")}</span>
          {scannerCount > 0 && <span className="wb-subtab-count">{scannerCount}</span>}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "alerts"}
          className={`wb-subtab-btn${activeTab === "alerts" ? " is-active" : ""}`}
          data-testid="monitor-tab-alerts"
          onClick={() => onTabChange("alerts")}
        >
          <span>{t("预警", "Alerts")}</span>
          {alertBadge > 0 && (
            <span
              className={`wb-subtab-count${alertsController.unread > 0 ? " is-alert" : ""}`}
            >
              {alertBadge}
            </span>
          )}
        </button>
      </div>

      <div className="wb-panel-shell-body">
        {activeTab === "scanner" ? (
          <ScannerPanel
            controller={scannerController}
            onLocate={onLocate}
            readOnly={readOnly}
          />
        ) : (
          <AlertWorkbench
            controller={alertsController}
            deniedAlerts={deniedAlerts}
            instrumentId={instrumentId}
            timeframe={timeframe}
            instruments={instruments}
            initialDrawings={drawings}
            onLocate={onLocate}
            link={link}
            formSeed={alertSeed}
            onSeedConsumed={onSeedConsumed}
            replayActive={replayActive}
          />
        )}
      </div>
    </div>
  );
}
