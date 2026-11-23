import { useState } from "react";
import type { KeyLevel, KeyLevelCategory } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import {
  EVIDENCE_CLASSES,
  KEY_LEVEL_CATEGORIES,
  categoryMeta,
  countByCategory,
  countByEvidence,
  evidenceMeta,
  formatLevelPrice,
  keyLevelKindLabel,
  keyLevelStatusLabel,
  levelEvidenceClass,
  levelTitle,
  selectedEvidenceClasses,
  showLevelActions,
  toggleCategory,
  toggleEvidenceClass,
  type KeyLevelFilter,
} from "./model";

export interface KeyLevelPanelProps {
  levels: KeyLevel[];
  allLevels: KeyLevel[];
  filter: KeyLevelFilter;
  onFilterChange: (next: KeyLevelFilter) => void;
  precision: number;
  ignoredCount: number;
  onIgnore: (levelId: string) => void;
  ignoringId: string | null;
  onAdopt: (level: KeyLevel) => void;
  adoptingId: string | null;
  adoptedIds: Set<string>;
  onAttachAlert: (level: KeyLevel) => void;
  loading: boolean;
  error: string | null;
  readOnly: boolean;
  onClose?: () => void;
  notes?: string[];
}

function statusClass(status: KeyLevel["status"]): string {
  return `key-level-status key-level-status--${status.toLowerCase()}`;
}

export function KeyLevelPanel({
  levels,
  allLevels,
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
  onClose,
  notes = [],
}: KeyLevelPanelProps) {
  useLocale();
  const [menuLevelId, setMenuLevelId] = useState<string | null>(null);
  const counts = countByCategory(allLevels);
  const evidenceCounts = countByEvidence(allLevels);
  const strengthValue = Math.round(filter.minStrength * 100);

  return (
    <aside
      className="key-level-panel"
      data-testid="key-level-panel"
      aria-label={t("关键位过滤面板", "Key level filter panel")}
    >
      <header className="key-level-panel-head">
        <div>
          <strong>{t("关键位", "Key levels")}</strong>
          <span className="key-level-count" data-testid="key-level-count">
            {levels.length} / {allLevels.length}
          </span>
        </div>
        {onClose !== undefined && (
          <button
            type="button"
            className="key-level-close"
            aria-label={t("收起关键位面板", "Collapse key level panel")}
            onClick={onClose}
          >
            ×
          </button>
        )}
      </header>

      {readOnly && (
        <p className="key-level-readonly" data-testid="key-level-readonly">
          {t("只读视图 · 筛选可查看，不可修改", "Read-only view · Filtering is available, changes are not")}
        </p>
      )}

      <section className="key-level-filters" aria-label={t("关键位筛选", "Key level filters")}>
        <div className="key-level-category-row" role="group" aria-label={t("关键位类别", "Key level categories")}>
          {KEY_LEVEL_CATEGORIES.map((category: KeyLevelCategory) => {
            const meta = categoryMeta(category);
            const selected = filter.categories.includes(category);
            return (
              <button
                key={category}
                type="button"
                className={`key-level-chip${selected ? " is-active" : ""}`}
                aria-pressed={selected}
                disabled={readOnly}
                data-testid={`key-level-category-${category}`}
                onClick={() =>
                  onFilterChange(toggleCategory(filter, category))
                }
              >
                <span
                  className="key-level-swatch"
                  style={{ background: meta.color }}
                  aria-hidden="true"
                />
                {meta.label}
                <span className="key-level-chip-count">{counts[category]}</span>
              </button>
            );
          })}
        </div>

        <div
          className="key-level-evidence-row"
          role="group"
          aria-label={t("证据类别", "Evidence classes")}
        >
          {EVIDENCE_CLASSES.map((evidence) => {
            const selected = selectedEvidenceClasses(filter).includes(evidence);
            return (
              <button
                key={evidence}
                type="button"
                className={`key-level-evidence-chip${
                  selected ? " is-active" : ""
                }`}
                aria-pressed={selected}
                disabled={readOnly}
                data-testid={`key-level-evidence-${evidence}`}
                onClick={() =>
                  onFilterChange(toggleEvidenceClass(filter, evidence))
                }
              >
                <span
                  className={`key-level-evidence-mark key-level-evidence-mark--${evidence.toLowerCase()}`}
                  aria-hidden="true"
                />
                {evidenceMeta(evidence).label}
                <span className="key-level-chip-count">
                  {evidenceCounts[evidence]}
                </span>
              </button>
            );
          })}
        </div>

        <label className="key-level-strength">
          <span>
            {t("强度阈值", "Strength threshold")}{" "}
            <output data-testid="key-level-strength-value">{filter.minStrength.toFixed(2)}</output>
          </span>
          <input
            type="range"
            min={0}
            max={90}
            step={5}
            value={strengthValue}
            disabled={readOnly}
            aria-label={t("最低强度阈值", "Minimum strength threshold")}
            data-testid="key-level-strength"
            onChange={(event) =>
              onFilterChange({
                ...filter,
                minStrength: Number(event.target.value) / 100,
              })
            }
          />
        </label>

        <div className="key-level-legend" aria-label={t("关键位图例", "Key level legend")}>
          {KEY_LEVEL_CATEGORIES.map((category) => (
            <span key={category} className="key-level-legend-item">
              <span
                className="key-level-swatch"
                style={{ background: categoryMeta(category).color }}
                aria-hidden="true"
              />
              {categoryMeta(category).label}
            </span>
          ))}
          <span className="key-level-legend-item key-level-legend-item--muted">
            <span className="key-level-line-sample" aria-hidden="true" />
            {t("观测 实线", "Observed solid")}
          </span>
          <span className="key-level-legend-item key-level-legend-item--muted">
            <span
              className="key-level-line-sample key-level-line-sample--estimated"
              aria-hidden="true"
            />
            {t("估算 虚线/半透明", "Estimated dashed/translucent")}
          </span>
          <span className="key-level-legend-item key-level-legend-item--muted">
            {t("虚线=已破/缺口", "Dashed = broken/gap")}
          </span>
        </div>
      </section>

      {error !== null && (
        <p className="key-level-error" role="alert">
          {error}
        </p>
      )}

      {notes.length > 0 && (
        <ul className="key-level-notes" data-testid="key-level-notes">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}

      {loading ? (
        <p className="key-level-hint">{t("正在计算关键位…", "Computing key levels…")}</p>
      ) : levels.length === 0 ? (
        <div className="key-level-empty" data-testid="key-level-empty">
          <strong>{t("当前筛选没有匹配的关键位", "No key levels match the current filter")}</strong>
          <span>{t("降低强度阈值或勾选更多类别后再试。", "Lower the strength threshold or select more categories.")}</span>
        </div>
      ) : (
        <ul className="key-level-list" data-testid="key-level-list">
          {levels.map((level) => (
            <li
              key={level.level_id}
              className="key-level-row"
              data-testid={`key-level-row-${level.level_id}`}
            >
              <span
                className="key-level-dot"
                style={{ background: categoryMeta(level.category).color }}
                aria-hidden="true"
              />
              <div className="key-level-row-main">
                <span className="key-level-row-title">
                  {keyLevelKindLabel(level.kind)}
                  <span className={statusClass(level.status)}>
                    {keyLevelStatusLabel(level.status)}
                  </span>
                  {levelEvidenceClass(level) === "ESTIMATED" && (
                    <span
                      className="key-level-evidence-badge"
                      data-testid={`key-level-estimated-${level.level_id}`}
                    >
                      {t("估算", "Est.")}
                    </span>
                  )}
                </span>
                <span className="key-level-row-price">
                  {formatLevelPrice(level, precision)}
                </span>
                <span className="key-level-row-meta">
                  {t("强度", "Strength")} {level.strength.toFixed(2)} ·{" "}
                  {levelEvidenceClass(level) === "ESTIMATED"
                    ? t("OI 推算", "OI estimate")
                    : t(`触及 ${level.touch_count}`, `Touches ${level.touch_count}`)}
                </span>
              </div>
              {/* 采纳写路径走 DrawingService（复制为独立人工画线），忽略仍走质量反馈。 */}
              {adoptedIds.has(level.level_id) && (
                <span
                  className="key-level-adopted-badge"
                  data-testid={`key-level-adopted-${level.level_id}`}
                >
                  {t("已采纳", "Adopted")}
                </span>
              )}
              {showLevelActions(readOnly) && (
                <div className="key-level-actions">
                  <button
                    type="button"
                    className="key-level-actions-toggle"
                    title={levelTitle(level, precision)}
                    aria-label={t(`关键位操作 ${levelTitle(level, precision)}`, `Key level actions ${levelTitle(level, precision)}`)}
                    aria-haspopup="menu"
                    aria-expanded={menuLevelId === level.level_id}
                    onClick={() =>
                      setMenuLevelId((current) =>
                        current === level.level_id ? null : level.level_id,
                      )
                    }
                  >
                    ⋯
                  </button>
                </div>
              )}
              {showLevelActions(readOnly) && menuLevelId === level.level_id && (
                <div
                  className="key-level-actions-menu"
                  role="menu"
                  data-testid={`key-level-actions-${level.level_id}`}
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenuLevelId(null);
                      onAttachAlert(level);
                    }}
                  >
                    {t("挂预警", "Attach alert")}
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    disabled={
                      adoptingId !== null ||
                      adoptedIds.has(level.level_id)
                    }
                    onClick={() => {
                      setMenuLevelId(null);
                      onAdopt(level);
                    }}
                  >
                    {adoptedIds.has(level.level_id)
                      ? t("已采纳", "Adopted")
                      : adoptingId === level.level_id
                        ? t("采纳中…", "Adopting…")
                        : t("采纳为画线", "Adopt as drawing")}
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    disabled={ignoringId !== null}
                    onClick={() => {
                      setMenuLevelId(null);
                      onIgnore(level.level_id);
                    }}
                  >
                    {ignoringId === level.level_id ? t("忽略中…", "Ignoring…") : t("忽略", "Ignore")}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {ignoredCount > 0 && (
        <p className="key-level-ignored-note" data-testid="key-level-ignored-note">
          {t(
            `已忽略 ${ignoredCount} 个关键位（刷新后仍保持隐藏）`,
            `${ignoredCount} key levels ignored (stay hidden after refresh)`,
          )}
        </p>
      )}
    </aside>
  );
}
