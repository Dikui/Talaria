import { useEffect, useReducer } from "react";
import type { Chart } from "klinecharts";
import type { KeyLevel } from "@talaria/contracts";

import { t, useLocale } from "../../lib/i18n";
import { frameSignature, readChartFrame } from "../drawings/projection";
import {
  categoryMeta,
  isZoneLevel,
  layoutKeyLevelLabels,
  levelEvidenceClass,
  levelLabel,
  levelPrice,
  levelTitle,
  statusDash,
} from "./model";

export interface KeyLevelLayerProps {
  chart: Chart | null;
  levels: KeyLevel[];
  pricePrecision: number;
  /** 右侧过滤面板打开时把标签向左让位。 */
  rightInset?: number;
}

interface ProjectedLevel {
  level: KeyLevel;
  y: number;
  top: number | null;
  bottom: number | null;
}

interface PixelValue {
  y?: number;
}

function projectPrices(chart: Chart, prices: number[]): Array<number | null> {
  const raw = chart.convertToPixel(
    prices.map((value) => ({ value })),
    { absolute: true },
  ) as PixelValue | PixelValue[] | null;
  const points = Array.isArray(raw) ? raw : raw == null ? [] : [raw];
  return prices.map((_, index) => {
    const y = points[index]?.y;
    return typeof y === "number" && Number.isFinite(y) ? y : null;
  });
}

export function KeyLevelLayer({
  chart,
  levels,
  pricePrecision,
  rightInset = 0,
}: KeyLevelLayerProps) {
  useLocale();
  const [, forceRender] = useReducer((value: number) => value + 1, 0);

  // 与画线覆盖层相同：KLineChart 的缩放/平移不保证发事件，用逐帧签名兜底。
  useEffect(() => {
    if (chart == null) {
      return;
    }
    let raf = 0;
    let last = "";
    const tick = () => {
      const next = frameSignature(chart);
      if (next !== last) {
        last = next;
        forceRender();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [chart]);

  if (chart == null || levels.length === 0) {
    return null;
  }
  const frame = readChartFrame(chart);
  if (frame == null) {
    return null;
  }
  const paneTop = frame.paneTop;
  const paneBottom = frame.paneTop + frame.paneHeight;
  const prices = levels.flatMap((level) =>
    isZoneLevel(level)
      ? [Number(level.price_low), Number(level.price_high)]
      : [levelPrice(level)],
  );
  const projections = projectPrices(chart, prices);
  let cursor = 0;
  const projected: ProjectedLevel[] = [];
  for (const level of levels) {
    const price = levelPrice(level);
    if (isZoneLevel(level)) {
      const lowY = projections[cursor++];
      const highY = projections[cursor++];
      if (lowY == null || highY == null) {
        continue;
      }
      projected.push({
        level,
        y: (lowY + highY) / 2,
        top: Math.min(lowY, highY),
        bottom: Math.max(lowY, highY),
      });
    } else {
      const y = projections[cursor++];
      if (y == null) {
        continue;
      }
      projected.push({ level, y, top: null, bottom: null });
    }
  }
  const inPane = projected.filter(
    (item) => item.y >= paneTop - 2 && item.y <= paneBottom + 2,
  );
  if (inPane.length === 0) {
    return null;
  }

  // 标签只服务最靠前的少量位置；其余关键位仍画线并在面板列表中可读。
  // 窄屏顶部有标的/时间信息，且主图高度有限，先算可容纳的标签数，避免相互压叠。
  const labelTop = paneTop + (frame.width < 520 ? 52 : 0);
  const labelCapacity = Math.max(
    2,
    Math.floor((paneBottom - labelTop - 8) / 22),
  );
  const labelLevels = frame.width < 420
    ? []
    : inPane
        .slice()
        .sort((left, right) => right.level.strength - left.level.strength)
        .slice(0, Math.min(12, labelCapacity));
  const labels = layoutKeyLevelLabels(
    labelLevels.map((item) => ({
      id: item.level.level_id,
      text: levelLabel(item.level, pricePrecision),
      y: item.y,
    })),
    {
      width: frame.width,
      height: frame.height,
      top: labelTop,
      bottom: paneBottom,
      rightInset,
    },
  );
  const labelById = new Map(labels.map((label) => [label.id, label]));
  const estimatedTag =
    frame.width >= 480
      ? inPane
          .filter((item) => levelEvidenceClass(item.level) === "ESTIMATED")
          .sort(
            (left, right) => right.level.strength - left.level.strength,
          )[0]
      : undefined;

  return (
    <div className="key-level-overlay" data-testid="key-level-overlay">
      <svg
        className="key-level-surface"
        width={frame.width}
        height={frame.height}
        viewBox={`0 0 ${frame.width} ${frame.height}`}
        aria-hidden="true"
      >
        {inPane.map(({ level, y, top, bottom }) => {
          const meta = categoryMeta(level.category);
          const dash = statusDash(level.status);
          const estimated = levelEvidenceClass(level) === "ESTIMATED";
          return (
            <g
              key={level.level_id}
              data-testid={`key-level-${level.level_id}`}
              data-category={level.category}
              data-status={level.status}
              data-evidence={levelEvidenceClass(level)}
            >
              <line
                x1={0}
                x2={frame.width}
                y1={y}
                y2={y}
                stroke={meta.color}
                strokeWidth={
                  estimated ? 1.2 : level.strength >= 0.7 ? 1.6 : 1
                }
                strokeDasharray={
                  estimated ? "3 3" : level.kind === "GAP" ? "8 5" : dash
                }
                strokeOpacity={
                  estimated ? 0.5 : level.status === "ACTIVE" ? 0.85 : 0.55
                }
              />
              {top != null && bottom != null && (
                <rect
                  x={0}
                  y={top}
                  width={frame.width}
                  height={Math.max(2, bottom - top)}
                  fill={meta.color}
                  fillOpacity={estimated ? 0.05 : 0.08}
                  stroke={meta.color}
                  strokeOpacity={estimated ? 0.45 : 0.35}
                  strokeDasharray={estimated ? "3 3" : "4 4"}
                />
              )}
            </g>
          );
        })}
        {estimatedTag !== undefined && (
          <g
            className="key-level-estimated-tag"
            data-testid="key-level-estimated-tag"
          >
            <rect
              x={frame.width - rightInset - 78}
              y={20}
              width={70}
              height={14}
              rx={3}
              fill="var(--surface)"
              fillOpacity={0.9}
              stroke={categoryMeta(estimatedTag.level.category).color}
              strokeOpacity={0.5}
              strokeDasharray="3 3"
            />
            <text
              x={frame.width - rightInset - 74}
              y={31}
              fill={categoryMeta(estimatedTag.level.category).color}
            >
              {t("估算", "ESTIMATED")}
            </text>
          </g>
        )}
        {labels.map((label) => {
          const item = inPane.find(
            (entry) => entry.level.level_id === label.id,
          );
          const level = item?.level ?? labelLevels[0]?.level;
          if (level === undefined) {
            return null;
          }
          const meta = categoryMeta(level.category);
          const estimated = levelEvidenceClass(level) === "ESTIMATED";
          const left =
            label.anchor === "end" ? label.x - label.width : label.x;
          return (
            <g
              key={`label-${label.id}`}
              className="key-level-label"
              data-testid={`key-level-label-${label.id}`}
            >
              <title>{levelTitle(level, pricePrecision)}</title>
              <rect
                x={left}
                y={label.y}
                width={label.width}
                height={label.height}
                rx={3}
                fill={meta.color}
                fillOpacity={estimated ? 0.1 : 0.16}
                stroke={meta.color}
                strokeOpacity={0.7}
                strokeDasharray={estimated ? "3 3" : undefined}
              />
              <text
                x={label.anchor === "end" ? label.x - 4 : label.x + 4}
                y={label.y + 13}
                textAnchor={label.anchor}
                fill={meta.color}
              >
                {label.text}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
