import type { KeyLevel, KeyLevelCategory } from "@talaria/contracts";

import {
  CATEGORY_META,
  KEY_LEVEL_CATEGORIES,
  KIND_LABELS,
  STATUS_LABELS,
  countByCategory,
  formatLevelPrice,
  levelTitle,
  toggleCategory,
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
  loading: boolean;
  error: string | null;
  readOnly: boolean;
  onClose: () => void;
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
  loading,
  error,
  readOnly,
  onClose,
}: KeyLevelPanelProps) {
  const counts = countByCategory(allLevels);
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
        <button
          type="button"
          className="key-level-close"
          aria-label="收起关键位面板"
          onClick={onClose}
        >
          ×
        </button>
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
            虚线=已破/缺口
          </span>
        </div>
      </section>

      {error !== null && (
        <p className="key-level-error" role="alert">
          {error}
        </p>
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
                </span>
                <span className="key-level-row-price">
                  {formatLevelPrice(level, precision)}
                </span>
                <span className="key-level-row-meta">
                  强度 {level.strength.toFixed(2)} · 触及 {level.touch_count}
                </span>
              </div>
              {/* 14 的“采纳为人工画线”入口预留在此行尾部；写路径走 DrawingService，不混入忽略反馈。 */}
              {!readOnly && (
                <button
                  type="button"
                  className="key-level-ignore"
                  title={levelTitle(level, precision)}
                  aria-label={`忽略 ${levelTitle(level, precision)}`}
                  disabled={ignoringId !== null}
                  onClick={() => onIgnore(level.level_id)}
                >
                  {ignoringId === level.level_id ? "…" : "忽略"}
                </button>
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
