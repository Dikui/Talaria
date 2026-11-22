import { useState } from "react";
import type { KeyLevel, KeyLevelCategory } from "@talaria/contracts";

import {
  CATEGORY_META,
  EVIDENCE_CLASSES,
  EVIDENCE_META,
  KEY_LEVEL_CATEGORIES,
  KIND_LABELS,
  STATUS_LABELS,
  countByCategory,
  countByEvidence,
  formatLevelPrice,
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
  const [menuLevelId, setMenuLevelId] = useState<string | null>(null);
  const counts = countByCategory(allLevels);
  const evidenceCounts = countByEvidence(allLevels);
  const strengthValue = Math.round(filter.minStrength * 100);

  return (
    <aside
      className="key-level-panel"
      data-testid="key-level-panel"
      aria-label="关键位过滤面板"
    >
      <header className="key-level-panel-head">
        <div>
          <strong>关键位</strong>
          <span className="key-level-count" data-testid="key-level-count">
            {levels.length} / {allLevels.length}
          </span>
        </div>
        {onClose !== undefined && (
          <button
            type="button"
            className="key-level-close"
            aria-label="收起关键位面板"
            onClick={onClose}
          >
            ×
          </button>
        )}
      </header>

      {readOnly && (
        <p className="key-level-readonly" data-testid="key-level-readonly">
          只读视图 · 筛选可查看，不可修改
        </p>
      )}

      <section className="key-level-filters" aria-label="关键位筛选">
        <div className="key-level-category-row" role="group" aria-label="关键位类别">
          {KEY_LEVEL_CATEGORIES.map((category: KeyLevelCategory) => {
            const meta = CATEGORY_META[category];
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
          aria-label="证据类别"
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
                {EVIDENCE_META[evidence].label}
                <span className="key-level-chip-count">
                  {evidenceCounts[evidence]}
                </span>
              </button>
            );
          })}
        </div>

        <label className="key-level-strength">
          <span>
            强度阈值 <output data-testid="key-level-strength-value">{filter.minStrength.toFixed(2)}</output>
          </span>
          <input
            type="range"
            min={0}
            max={90}
            step={5}
            value={strengthValue}
            disabled={readOnly}
            aria-label="最低强度阈值"
            data-testid="key-level-strength"
            onChange={(event) =>
              onFilterChange({
                ...filter,
                minStrength: Number(event.target.value) / 100,
              })
            }
          />
        </label>

        <div className="key-level-legend" aria-label="关键位图例">
          {KEY_LEVEL_CATEGORIES.map((category) => (
            <span key={category} className="key-level-legend-item">
              <span
                className="key-level-swatch"
                style={{ background: CATEGORY_META[category].color }}
                aria-hidden="true"
              />
              {CATEGORY_META[category].label}
            </span>
          ))}
          <span className="key-level-legend-item key-level-legend-item--muted">
            <span className="key-level-line-sample" aria-hidden="true" />
            观测 实线
          </span>
          <span className="key-level-legend-item key-level-legend-item--muted">
            <span
              className="key-level-line-sample key-level-line-sample--estimated"
              aria-hidden="true"
            />
            估算 虚线/半透明
          </span>
          <span className="key-level-legend-item key-level-legend-item--muted">
            虚线=已破/缺口
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
        <p className="key-level-hint">正在计算关键位…</p>
      ) : levels.length === 0 ? (
        <div className="key-level-empty" data-testid="key-level-empty">
          <strong>当前筛选没有匹配的关键位</strong>
          <span>降低强度阈值或勾选更多类别后再试。</span>
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
                style={{ background: CATEGORY_META[level.category].color }}
                aria-hidden="true"
              />
              <div className="key-level-row-main">
                <span className="key-level-row-title">
                  {KIND_LABELS[level.kind]}
                  <span className={statusClass(level.status)}>
                    {STATUS_LABELS[level.status]}
                  </span>
                  {levelEvidenceClass(level) === "ESTIMATED" && (
                    <span
                      className="key-level-evidence-badge"
                      data-testid={`key-level-estimated-${level.level_id}`}
                    >
                      估算
                    </span>
                  )}
                </span>
                <span className="key-level-row-price">
                  {formatLevelPrice(level, precision)}
                </span>
                <span className="key-level-row-meta">
                  强度 {level.strength.toFixed(2)} ·{" "}
                  {levelEvidenceClass(level) === "ESTIMATED"
                    ? "OI 推算"
                    : `触及 ${level.touch_count}`}
                </span>
              </div>
              {/* 采纳写路径走 DrawingService（复制为独立人工画线），忽略仍走质量反馈。 */}
              {adoptedIds.has(level.level_id) && (
                <span
                  className="key-level-adopted-badge"
                  data-testid={`key-level-adopted-${level.level_id}`}
                >
                  已采纳
                </span>
              )}
              {showLevelActions(readOnly) && (
                <div className="key-level-actions">
                  <button
                    type="button"
                    className="key-level-actions-toggle"
                    title={levelTitle(level, precision)}
                    aria-label={`关键位操作 ${levelTitle(level, precision)}`}
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
                    挂预警
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
                      ? "已采纳"
                      : adoptingId === level.level_id
                        ? "采纳中…"
                        : "采纳为画线"}
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
                    {ignoringId === level.level_id ? "忽略中…" : "忽略"}
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {ignoredCount > 0 && (
        <p className="key-level-ignored-note" data-testid="key-level-ignored-note">
          已忽略 {ignoredCount} 个关键位（刷新后仍保持隐藏）
        </p>
      )}
    </aside>
  );
}
