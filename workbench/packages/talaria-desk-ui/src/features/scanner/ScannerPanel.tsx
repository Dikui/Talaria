import { useState } from "react";
import type { ScannerFocusItem, ScannerTrigger } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { PerspectiveTableSlot } from "../../workbench/PerspectiveTableSlot";
import {
  SCANNER_SEVERITY_RANK,
  evidenceLabel,
  focusLocatePayload,
  locatePayload,
  scannerKindLabel,
  scannerSeverityLabel,
  summaryNeedsCollapse,
  summaryPreview,
  triggerTags,
  triggerTime,
  type ScannerLocatePayload,
} from "./model";
import type { ScannerController } from "./useScanner";

export interface ScannerPanelProps {
  controller: ScannerController;
  onLocate: (payload: ScannerLocatePayload) => void;
  readOnly?: boolean;
  now?: number;
}

function FocusChips({
  items,
  onLocate,
}: {
  items: ScannerFocusItem[];
  onLocate: (payload: ScannerLocatePayload) => void;
}) {
  useLocale();
  const sorted = [...(Array.isArray(items) ? items : [])].sort(
    (left, right) =>
      SCANNER_SEVERITY_RANK[right.severity] - SCANNER_SEVERITY_RANK[left.severity] ||
      right.last_trigger_ts - left.last_trigger_ts,
  );
  if (sorted.length === 0) return null;
  return (
    <div className="scanner-focus" aria-label={t("扫描焦点", "Scanner focus")}>
      <span className="scanner-focus-label">{t("焦点", "Focus")}</span>
      <div className="scanner-focus-chips">
        {sorted.map((item) => {
          const payload = focusLocatePayload(item);
          return (
            <button
              key={item.instrument_id}
              type="button"
              className={`scanner-focus-chip severity-${item.severity.toLowerCase()}`}
              disabled={payload === null}
              onClick={() => payload !== null && onLocate(payload)}
            >
              <span>{item.instrument_id.replace("-USDT-PERP", "")}</span>
              <span className="chip-count">{item.trigger_count}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ScannerCard({
  trigger,
  onLocate,
  now,
}: {
  trigger: ScannerTrigger;
  onLocate: (payload: ScannerLocatePayload) => void;
  now?: number;
}) {
  useLocale();
  const [expanded, setExpanded] = useState(false);
  const tags = triggerTags(trigger);
  const collapsible = summaryNeedsCollapse(trigger);
  const summary =
    collapsible && !expanded ? summaryPreview(trigger.summary) : trigger.summary;
  const evidence = (trigger.evidence ?? []).map(evidenceLabel);

  return (
    <article
      className={`entity-card scanner-card severity-${trigger.severity.toLowerCase()}`}
      data-testid="scanner-card"
      role="button"
      tabIndex={0}
      onClick={() => onLocate(locatePayload(trigger))}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
          e.preventDefault();
          onLocate(locatePayload(trigger));
        }
      }}
    >
      <div className="entity-card-top">
        <span className="entity-tag">{scannerKindLabel(trigger.kind)}</span>
        <span className={`status-pill severity-${trigger.severity.toLowerCase()}`}>
          {scannerSeverityLabel(trigger.severity)}
        </span>
      </div>
      <div className="entity-card-main">
        <strong>
          {trigger.title} · {trigger.instrument_id.replace("-USDT-PERP", "")} {trigger.timeframe}
        </strong>
        <p className="entity-rationale">{summary}</p>
        <span className="entity-evidence">{triggerTime(trigger.created_ts, now)}</span>
      </div>
      {expanded && evidence.length > 0 && (
        <div className="scanner-evidence">
          {evidence.map((line, i) => (
            <span key={i} className="entity-evidence">
              {line}
            </span>
          ))}
        </div>
      )}
      <div className="scanner-tags">
        {tags.map((tag) => (
          <span key={tag} className="filter-chip">
            {tag}
          </span>
        ))}
        {collapsible && (
          <button
            type="button"
            className="ghost-button"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
          >
            {expanded ? t("收起", "Less") : t("展开", "More")}
          </button>
        )}
      </div>
    </article>
  );
}

export function ScannerPanel({
  controller,
  onLocate,
  readOnly = false,
  now,
}: ScannerPanelProps) {
  useLocale();
  const { triggers, focus, loading, scanning, error, scanNow } = controller;

  return (
    <section
      className="dock-panel scanner-panel"
      data-testid="scanner-dock"
      aria-label={t("异动扫描器", "Market scanner")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("数值扫描器", "Scanner")}</h2>
          <span className="dock-panel-meta">{triggers.length}</span>
        </div>
        {!readOnly && (
          <button
            type="button"
            className="ghost-button"
            disabled={scanning}
            onClick={() => void scanNow()}
          >
            {scanning ? t("扫描中…", "Scanning…") : t("立即扫描", "Scan now")}
          </button>
        )}
      </header>

      <FocusChips items={focus} onLocate={onLocate} />

      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}

      <PerspectiveTableSlot
        rows={triggers}
        tableName="scanner"
        fallback={
          loading && triggers.length === 0 ? (
            <p className="panel-empty">{t("加载扫描记录中…", "Loading scanner triggers…")}</p>
          ) : triggers.length === 0 ? (
            <p className="panel-empty">{t("暂无扫描异动。", "No scanner anomalies yet.")}</p>
          ) : (
            <div className="entity-list">
              {triggers.map((trigger) => (
                <ScannerCard
                  key={trigger.trigger_id}
                  trigger={trigger}
                  onLocate={onLocate}
                  now={now}
                />
              ))}
            </div>
          )
        }
      />
    </section>
  );
}
