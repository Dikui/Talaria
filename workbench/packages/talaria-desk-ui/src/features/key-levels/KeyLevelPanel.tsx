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
  levelTitle,
  selectedEvidenceClasses,
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
  notes?: string[];
  highlightedId?: string | null;
  onHover?: (levelId: string | null) => void;
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
  notes = [],
  highlightedId,
  onHover,
}: KeyLevelPanelProps) {
  useLocale();
  const counts = countByCategory(allLevels);
  const evidenceCounts = countByEvidence(allLevels);
  const strengthValue = Math.round(filter.minStrength * 100);

  return (
    <section
      className="dock-panel key-level-panel"
      data-testid="key-level-panel"
      aria-label={t("关键位", "Key levels")}
    >
      <header className="dock-panel-head">
        <div>
          <h2>{t("关键位 (只读图层)", "Key Levels (Read-only)")}</h2>
          <span className="dock-panel-meta" data-testid="key-level-count">
            {levels.length} / {allLevels.length}
            {ignoredCount > 0 ? ` · ${t(`已忽略 ${ignoredCount}`, `${ignoredCount} ignored`)}` : ""}
          </span>
        </div>
      </header>

      {readOnly && (
        <p className="panel-note" data-testid="key-level-readonly">
          {t("窄屏只读 · 筛选可查看，不可修改", "Read-only viewport · Filtering available")}
        </p>
      )}

      <div className="key-level-filters">
        <div className="chip-grid" role="group" aria-label={t("类别", "Categories")}>
          {KEY_LEVEL_CATEGORIES.map((category: KeyLevelCategory) => {
            const meta = categoryMeta(category);
            const selected = filter.categories.includes(category);
            return (
              <button
                key={category}
                type="button"
                className={`filter-chip${selected ? " is-active" : ""}`}
                aria-pressed={selected}
                disabled={readOnly}
                data-testid={`key-level-category-${category}`}
                onClick={() => onFilterChange(toggleCategory(filter, category))}
              >
                <span className="color-dot" style={{ background: meta.color }} />
                <span>{meta.label}</span>
                <span className="chip-count">{counts[category]}</span>
              </button>
            );
          })}
        </div>

        <div className="chip-grid" role="group" aria-label={t("证据", "Evidence")}>
          {EVIDENCE_CLASSES.map((evidence) => {
            const selected = selectedEvidenceClasses(filter).includes(evidence);
            return (
              <button
                key={evidence}
                type="button"
                className={`filter-chip${selected ? " is-active" : ""}`}
                aria-pressed={selected}
                disabled={readOnly}
                data-testid={`key-level-evidence-${evidence}`}
                onClick={() => onFilterChange(toggleEvidenceClass(filter, evidence))}
              >
                <span>{evidenceMeta(evidence).label}</span>
                <span className="chip-count">{evidenceCounts[evidence]}</span>
              </button>
            );
          })}
        </div>

        <label className="range-row">
          <span>
            {t("强度阈值", "Min strength")}{" "}
            <strong data-testid="key-level-strength-value">
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
            data-testid="key-level-strength"
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

      {loading && levels.length === 0 ? (
        <p className="panel-empty">{t("加载关键位中…", "Loading key levels…")}</p>
      ) : levels.length === 0 ? (
        <p className="panel-empty">{t("当前筛选条件无关键位。", "No matching key levels.")}</p>
      ) : (
        <ul className="entity-list">
          {levels.map((level) => {
            const meta = categoryMeta(level.category);
            const adopted = adoptedIds.has(level.level_id);
            const highlighted = highlightedId === level.level_id;
            return (
              <li
                key={level.level_id}
                className={`entity-card${highlighted ? " is-highlighted" : ""}`}
                title={levelTitle(level, precision)}
                onMouseEnter={() => onHover?.(level.level_id)}
                onMouseLeave={() => onHover?.(null)}
              >
                <div className="entity-card-top">
                  <span className="entity-tag">
                    <span className="color-dot" style={{ background: meta.color }} />
                    {keyLevelKindLabel(level.kind)}
                  </span>
                  <span className={`status-pill status-${level.status.toLowerCase()}`}>
                    {keyLevelStatusLabel(level.status)}
                  </span>
                </div>
                <div className="entity-card-main">
                  <strong>{formatLevelPrice(level, precision)}</strong>
                  <span>
                    {t("强度", "Str")} {(level.strength * 100).toFixed(0)}% ·{" "}
                    {t("触及", "Touch")} {level.touch_count}
                  </span>
                </div>
                {!readOnly && (
                  <div className="entity-actions">
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={adoptingId === level.level_id || adopted}
                      onClick={() => onAdopt(level)}
                    >
                      {adopted
                        ? t("已采纳", "Adopted")
                        : t("采纳为画线", "Adopt drawing")}
                    </button>
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={() => onAttachAlert(level)}
                    >
                      {t("设预警", "Alert")}
                    </button>
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={ignoringId === level.level_id}
                      onClick={() => onIgnore(level.level_id)}
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
