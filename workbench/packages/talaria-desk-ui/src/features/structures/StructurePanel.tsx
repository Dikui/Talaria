import type {
  StructureCategory,
  StructureFamily,
  StructureFeature,
} from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import {
  STRUCTURE_CATEGORIES,
  STRUCTURE_EVIDENCE_CLASSES,
  STRUCTURE_FAMILIES,
  countByFamily,
  familyMeta,
  formatStructureGeometry,
  higherTimeframesFor,
  isStructuresEnabled,
  structureCategoryMeta,
  structureStatusLabel,
  toggleDetectorId,
  toggleFamily,
  toggleHtfTimeframe,
  toggleStructureCategory,
  toggleStructureEvidence,
  toggleStructuresEnabled,
  type StructureFilter,
} from "./model";

export interface StructurePanelProps {
  structures: StructureFeature[];
  allStructures: StructureFeature[];
  activeTimeframe: string;
  filter: StructureFilter;
  onFilterChange: (next: StructureFilter) => void;
  precision: number;
  ignoredCount: number;
  onIgnore: (structureId: string) => void;
  ignoringId: string | null;
  onAdopt: (feature: StructureFeature) => void;
  adoptingId: string | null;
  adoptedIds: Set<string>;
  onAttachAlert: (feature: StructureFeature) => void;
  loading: boolean;
  error: string | null;
  readOnly: boolean;
  notes?: string[];
  highlightedId?: string | null;
  onHover?: (structureId: string | null) => void;
}

export function StructurePanel({
  structures,
  allStructures,
  activeTimeframe,
  filter,
  onFilterChange,
  precision,
  ignoredCount,
  onIgnore,
  ignoringId,
  onAdopt,
  adoptingId,
  adoptedIds,
  onAttachAlert,
  loading,
  error,
  readOnly,
  notes = [],
  highlightedId,
  onHover,
}: StructurePanelProps) {
  useLocale();
  const enabled = isStructuresEnabled(filter);
  const familyCounts = countByFamily(allStructures);
  const htfOptions = higherTimeframesFor(activeTimeframe);
  const availableDetectors = Array.from(
    new Set(allStructures.map((item) => item.detector_id)),
  ).sort();
  const strengthValue = Math.round(filter.minStrength * 100);

  return (
    <section
      className="dock-panel key-level-panel"
      data-testid="structure-panel"
      aria-label={t("市场结构", "Market structures")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("市场结构 (只读图层)", "Market Structures (Read-only)")}</h2>
          <span className="dock-panel-meta" data-testid="structure-count">
            {structures.length} / {allStructures.length}
            {ignoredCount > 0 ? ` · ${t(`已忽略 ${ignoredCount}`, `${ignoredCount} ignored`)}` : ""}
          </span>
        </div>
        <button
          type="button"
          className={`filter-chip${enabled ? " is-active" : ""}`}
          aria-pressed={enabled}
          data-testid="structure-panel-toggle"
          onClick={() => onFilterChange(toggleStructuresEnabled(filter))}
        >
          {enabled ? t("已开启", "Visible") : t("已关闭", "Hidden")}
        </button>
      </header>

      {readOnly && (
        <p className="panel-note" data-testid="structure-readonly">
          {t("窄屏只读 · 筛选可查看，不可修改", "Read-only viewport · Filtering available")}
        </p>
      )}

      <div className="key-level-filters">
        <div className="chip-grid" role="group" aria-label={t("视图模式", "View mode")}>
          <button
            type="button"
            className={`filter-chip${filter.viewMode === "simple" ? " is-active" : ""}`}
            aria-pressed={filter.viewMode === "simple"}
            data-testid="structure-mode-simple"
            onClick={() => onFilterChange({ ...filter, viewMode: "simple" })}
          >
            {t("简洁模式", "Simple")}
          </button>
          <button
            type="button"
            className={`filter-chip${filter.viewMode === "expert" ? " is-active" : ""}`}
            aria-pressed={filter.viewMode === "expert"}
            data-testid="structure-mode-expert"
            onClick={() => onFilterChange({ ...filter, viewMode: "expert" })}
          >
            {t("专家模式", "Expert")}
          </button>
          <button
            type="button"
            className={`filter-chip${filter.scale === "LINEAR" ? " is-active" : ""}`}
            aria-pressed={filter.scale === "LINEAR"}
            data-testid="structure-scale-linear"
            onClick={() => onFilterChange({ ...filter, scale: "LINEAR" })}
          >
            {t("线性", "Linear")}
          </button>
          <button
            type="button"
            className={`filter-chip${filter.scale === "LOG" ? " is-active" : ""}`}
            aria-pressed={filter.scale === "LOG"}
            data-testid="structure-scale-log"
            onClick={() => onFilterChange({ ...filter, scale: "LOG" })}
          >
            {t("对数", "Log")}
          </button>
        </div>

        {htfOptions.length > 0 && (
          <div className="chip-grid" role="group" aria-label={t("多周期共振 (HTF)", "MTFA timeframes")}>
            {htfOptions.map((tf) => {
              const selected = filter.htfTimeframes.includes(tf);
              return (
                <button
                  key={tf}
                  type="button"
                  className={`filter-chip${selected ? " is-active" : ""}`}
                  aria-pressed={selected}
                  data-testid={`structure-htf-${tf}`}
                  onClick={() => onFilterChange(toggleHtfTimeframe(filter, tf))}
                >
                  <span>HTF {tf}</span>
                </button>
              );
            })}
          </div>
        )}

        {filter.viewMode === "expert" && (
          <>
            <div className="chip-grid" role="group" aria-label={t("结构家族", "Families")}>
              {STRUCTURE_FAMILIES.map((family: StructureFamily) => {
                const meta = familyMeta(family);
                const selected = filter.families.includes(family);
                return (
                  <button
                    key={family}
                    type="button"
                    className={`filter-chip${selected ? " is-active" : ""}`}
                    aria-pressed={selected}
                    disabled={readOnly}
                    data-testid={`structure-family-${family}`}
                    onClick={() => onFilterChange(toggleFamily(filter, family))}
                  >
                    <span className="color-dot" style={{ background: meta.color }} />
                    <span>{meta.label}</span>
                    <span className="chip-count">{familyCounts[family]}</span>
                  </button>
                );
              })}
            </div>

            <div className="chip-grid" role="group" aria-label={t("类别", "Categories")}>
              {STRUCTURE_CATEGORIES.map((cat: StructureCategory) => {
                const meta = structureCategoryMeta(cat);
                const selected = filter.categories.includes(cat);
                return (
                  <button
                    key={cat}
                    type="button"
                    className={`filter-chip${selected ? " is-active" : ""}`}
                    aria-pressed={selected}
                    disabled={readOnly}
                    data-testid={`structure-category-${cat}`}
                    onClick={() => onFilterChange(toggleStructureCategory(filter, cat))}
                  >
                    <span className="color-dot" style={{ background: meta.color }} />
                    <span>{meta.label}</span>
                  </button>
                );
              })}
            </div>

            {availableDetectors.length > 0 && (
              <div className="chip-grid" role="group" aria-label={t("检测器", "Detectors")}>
                {availableDetectors.map((detId) => {
                  const selected =
                    filter.detectorIds.length === 0 || filter.detectorIds.includes(detId);
                  return (
                    <button
                      key={detId}
                      type="button"
                      className={`filter-chip${selected ? " is-active" : ""}`}
                      aria-pressed={selected}
                      disabled={readOnly}
                      onClick={() => onFilterChange(toggleDetectorId(filter, detId))}
                    >
                      <span>{detId}</span>
                    </button>
                  );
                })}
              </div>
            )}

            <div className="chip-grid" role="group" aria-label={t("证据", "Evidence")}>
              {STRUCTURE_EVIDENCE_CLASSES.map((ev) => {
                const selected = filter.evidenceClasses.includes(ev);
                return (
                  <button
                    key={ev}
                    type="button"
                    className={`filter-chip${selected ? " is-active" : ""}`}
                    aria-pressed={selected}
                    disabled={readOnly}
                    onClick={() => onFilterChange(toggleStructureEvidence(filter, ev))}
                  >
                    <span>{ev === "OBSERVED" ? t("观测", "Observed") : t("估算", "Estimated")}</span>
                  </button>
                );
              })}
            </div>
          </>
        )}

        <label className="range-row">
          <span>
            {t("强度阈值", "Min strength")}{" "}
            <strong data-testid="structure-strength-value">
              {filter.minStrength.toFixed(2)}
            </strong>
          </span>
          <input
            type="range"
            min={0}
            max={90}
            step={5}
            value={strengthValue}
            disabled={readOnly}
            data-testid="structure-strength"
            onChange={(e) =>
              onFilterChange({ ...filter, minStrength: Number(e.target.value) / 100 })
            }
          />
        </label>
      </div>

      {error && (
        <p className="panel-error" role="alert">
          {error}
        </p>
      )}
      {notes.length > 0 && (
        <ul className="panel-notes">
          {notes.map((note, idx) => (
            <li key={idx}>{note}</li>
          ))}
        </ul>
      )}

      {!enabled ? (
        <p className="panel-empty">
          {t(
            "市场结构图层已关闭，点击上方按钮或工具栏即可开启。",
            "Market structure layer is hidden. Toggle it on above or in the toolbar.",
          )}
        </p>
      ) : loading && structures.length === 0 ? (
        <p className="panel-empty">{t("加载市场结构中…", "Loading market structures…")}</p>
      ) : structures.length === 0 ? (
        <p className="panel-empty">{t("当前筛选条件无市场结构。", "No matching market structures.")}</p>
      ) : (
        <ul className="entity-list">
          {structures.map((item) => {
            const fMeta = familyMeta(item.family);
            const adopted = adoptedIds.has(item.structure_id);
            const highlighted = highlightedId === item.structure_id;
            const winTooltip = `${item.detector_id} · window ${new Date(
              item.analysis_window.from_ts,
            ).toISOString()} → ${new Date(item.analysis_window.to_ts).toISOString()}`;
            return (
              <li
                key={item.structure_id}
                className={`entity-card${highlighted ? " is-highlighted" : ""}`}
                title={winTooltip}
                onMouseEnter={() => onHover?.(item.structure_id)}
                onMouseLeave={() => onHover?.(null)}
              >
                <div className="entity-card-top">
                  <span className="entity-tag">
                    <span className="color-dot" style={{ background: fMeta.color }} />
                    {item.source_timeframe && item.source_timeframe !== item.timeframe
                      ? `[${item.source_timeframe}] `
                      : ""}
                    {item.kind}
                  </span>
                  <span className={`status-pill status-${item.status.toLowerCase()}`}>
                    {structureStatusLabel(item.status)}
                  </span>
                </div>
                <div className="entity-card-main">
                  <strong>{formatStructureGeometry(item, precision)}</strong>
                  <span>
                    {item.detector_id} · {t("强度", "Str")} {(item.strength * 100).toFixed(0)}% ·{" "}
                    {t("触及", "Touch")} {item.touch_count ?? 0}
                  </span>
                </div>
                {!readOnly && (
                  <div className="entity-actions">
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={adoptingId === item.structure_id || adopted}
                      onClick={() => onAdopt(item)}
                    >
                      {adopted
                        ? t("已采纳", "Adopted")
                        : t("采纳为画线", "Adopt drawing")}
                    </button>
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={() => onAttachAlert(item)}
                    >
                      {t("设预警", "Alert")}
                    </button>
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={ignoringId === item.structure_id}
                      onClick={() => onIgnore(item.structure_id)}
                    >
                      {t("忽略", "Ignore")}
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
