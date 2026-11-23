import { useState } from "react";
import type {
  ScannerFocusItem,
  ScannerSeverity,
  ScannerTrigger,
} from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import type { ScannerController } from "./useScanner";
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

export interface ScannerPanelProps {
  controller: ScannerController;
  open: boolean;
  onToggle?: () => void;
  onLocate: (payload: ScannerLocatePayload) => void;
  readOnly?: boolean;
  now?: number;
}

function SeverityBadge({ severity }: { severity: ScannerSeverity }) {
  useLocale();
  return (
    <span className={`scanner-severity severity-${severity.toLowerCase()}`}>
      {scannerSeverityLabel(severity)}
    </span>
  );
}

function FocusChips({
  items,
  onLocate,
}: {
  items: ScannerFocusItem[];
  onLocate: (payload: ScannerLocatePayload) => void;
}) {
  useLocale();
  const sorted = [...items].sort(
    (left, right) =>
      SCANNER_SEVERITY_RANK[right.severity] -
        SCANNER_SEVERITY_RANK[left.severity] ||
      right.last_trigger_ts - left.last_trigger_ts,
  );
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
              onClick={() => {
                if (payload !== null) {
                  onLocate(payload);
                }
              }}
              title={item.last_title ?? item.instrument_id}
            >
              <span className="scanner-focus-name">
                {item.instrument_id.replace("-USDT-PERP", "")}
              </span>
              <span className="scanner-focus-count">{item.trigger_count}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function ScannerCard({
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
      className={`scanner-card severity-${trigger.severity.toLowerCase()}`}
      data-testid="scanner-card"
      role="button"
      tabIndex={0}
      aria-label={t(`${scannerKindLabel(trigger.kind)} ${trigger.instrument_id}，定位到触发快照`, `${scannerKindLabel(trigger.kind)} ${trigger.instrument_id}, locate trigger snapshot`)}
      onClick={() => onLocate(locatePayload(trigger))}
      onKeyDown={(event) => {
        if (
          (event.key === "Enter" || event.key === " ") &&
          event.target === event.currentTarget
        ) {
          event.preventDefault();
          onLocate(locatePayload(trigger));
        }
      }}
    >
      <div className="scanner-card-top">
        <span className="scanner-kind">{scannerKindLabel(trigger.kind)}</span>
        <SeverityBadge severity={trigger.severity} />
        <time
          className="scanner-time"
          dateTime={new Date(trigger.created_ts).toISOString()}
        >
          {triggerTime(trigger.created_ts, now)}
        </time>
      </div>
      <div className="scanner-card-title">
        <span className="scanner-title-text">{trigger.title}</span>
        <span className="scanner-instrument">
          {trigger.instrument_id.replace("-USDT-PERP", "")} · {trigger.timeframe}
        </span>
      </div>
      <p
        className={`scanner-summary${collapsible && !expanded ? " is-clamped" : ""}`}
        data-testid="scanner-summary"
      >
        {summary}
      </p>
      {expanded && evidence.length > 0 && (
        <dl className="scanner-evidence" data-testid="scanner-evidence">
          {evidence.map((line, index) => (
            <div key={`${line}-${index}`}>
              <dt>{line.split(" ")[0]}</dt>
              <dd>{line.split(" ").slice(1).join(" ")}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="scanner-tags">
        {tags.map((tag) => (
          <span key={tag} className="scanner-tag">
            {tag}
          </span>
        ))}
      </div>
      {collapsible && (
        <button
          type="button"
          className="scanner-expand"
          aria-expanded={expanded}
          onClick={(event) => {
            event.stopPropagation();
            setExpanded((value) => !value);
          }}
        >
          {expanded ? t("收起摘要", "Collapse summary") : t("展开摘要", "Expand summary")}
        </button>
      )}
    </article>
  );
}

export function ScannerPanel({
  controller,
  open,
  onToggle,
  onLocate,
  readOnly = false,
  now,
}: ScannerPanelProps) {
  useLocale();
  const { triggers, focus, loading, scanning, error, unread } = controller;
  if (!open) {
    return null;
  }

  return (
    <section
      className="scanner-dock is-open"
      data-testid="scanner-dock"
      aria-label={t("数值扫描异动", "Numeric scanner anomalies")}
    >
      <header className="scanner-head">
        <div className="scanner-heading">
          <span className="scanner-kicker">{t("SCANNER · 纯数值 · 零模型调用", "SCANNER · Numeric only · Zero model calls")}</span>
          <h2 className="scanner-title">
            {t("盘面异动", "Market anomalies")}
            <span className="scanner-total" data-testid="scanner-total">
              {triggers.length}
            </span>
            {unread > 0 && (
              <span className="scanner-unread" aria-label={t(`${unread} 条未读异动`, `${unread} unread anomalies`)}>
                +{unread}
              </span>
            )}
          </h2>
        </div>
        <div className="scanner-head-actions">
          {!readOnly && (
            <button
              type="button"
              className="ghost-button scanner-scan-button"
              onClick={() => void controller.scanNow()}
              disabled={scanning}
            >
              {scanning ? t("扫描中…", "Scanning…") : t("手动扫描", "Scan now")}
            </button>
          )}
          {onToggle !== undefined && (
            <button
              type="button"
              className="ghost-button scanner-toggle"
              aria-expanded="true"
              onClick={onToggle}
            >
              {t("收起", "Collapse")}
            </button>
          )}
        </div>
      </header>

      {error !== null && (
        <div className="scanner-error" role="alert" data-testid="scanner-error">
          <span>{error}</span>
          <button
            type="button"
            aria-label={t("关闭扫描错误", "Dismiss scanner error")}
            onClick={controller.clearError}
          >
            ×
          </button>
        </div>
      )}

      {focus !== null && focus.items.length > 0 && (
        <FocusChips items={focus.items} onLocate={onLocate} />
      )}

      {loading && triggers.length === 0 ? (
        <div className="scanner-loading" data-testid="scanner-loading">
          {t("正在读取扫描结果…", "Loading scan results…")}
        </div>
      ) : triggers.length === 0 ? (
        <div className="scanner-empty" data-testid="scanner-empty" role="status">
          <span className="scanner-empty-mark" aria-hidden="true">
            ≈
          </span>
          <div>
            <strong>{t("盘面平静，暂无结构性异动", "Markets are calm — no structural anomalies")}</strong>
            <p>{t("扫描器会持续检查流动性、波动率、资金费率、OI 与关键位。", "The scanner continuously checks liquidity, volatility, funding, OI and key levels.")}</p>
          </div>
        </div>
      ) : (
        <div className="scanner-list" data-testid="scanner-list">
          {triggers.map((trigger) => (
            <ScannerCard
              key={trigger.trigger_id}
              trigger={trigger}
              onLocate={onLocate}
              now={now}
            />
          ))}
        </div>
      )}
    </section>
  );
}
