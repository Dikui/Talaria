import { useCallback, useEffect, useRef, type ReactNode } from "react";
import {
  dispose,
  init,
  registerIndicator,
  type Chart,
  type DataLoader,
  type DataLoaderSubscribeBarParams,
  type DataLoaderUnsubscribeBarParams,
  type IndicatorCreate,
  type IndicatorFigure,
  type KLineData,
} from "klinecharts";

import type {
  CompactBar,
  IndicatorSeries,
  KlineEvent,
} from "@talaria/contracts";

import { toKLineData, toKLineSeries, toPeriod } from "../../lib/chartData";
import { alignIndicatorRows } from "../../lib/indicatorData";
import {
  INDICATOR_CHANNELS,
  INDICATOR_COLORS,
} from "../indicators/config";
import type { Theme } from "../../lib/theme";

import { shouldDeliverLiveBar } from "./liveDelivery";

type IndicatorRow = Record<string, number | null>;

const OVERLAY_TEMPLATE = "talaria_overlay";
const STUDY_TEMPLATE = "talaria_study";
const INDICATOR_ID_PREFIX = "talaria-";
/** KLineChart 主图（K 线）窗格的固定 id；主图指标必须显式挂到这里。 */
const CANDLE_PANE_ID = "candle_pane";

/**
 * 只为 KLineChart 注册一次模板；所有指标数学都在 Python。
 * 每次数据更新只替换 calc 闭包，由 overrideIndicator 触发重算。
 */
registerIndicator<IndicatorRow>({
  name: OVERLAY_TEMPLATE,
  shortName: "指标",
  series: "price",
  figures: [{ key: "value", title: "值", type: "line" }],
  calc: () => [],
});
registerIndicator<IndicatorRow>({
  name: STUDY_TEMPLATE,
  shortName: "指标",
  series: "normal",
  figures: [{ key: "value", title: "值", type: "line" }],
  calc: () => [],
});

function figuresFor(series: IndicatorSeries): IndicatorFigure[] {
  const channels = INDICATOR_CHANNELS[series.name];
  return channels.map((channel) => ({
    key: channel,
    title:
      series.name === "BOLL"
        ? { mid: "中轨", upper: "上轨", lower: "下轨" }[channel]
        : series.name,
    type: "line",
    styles: () => ({ color: INDICATOR_COLORS[series.name] }),
  }));
}

function calcFor(series: IndicatorSeries) {
  return (dataList: KLineData[]): IndicatorRow[] =>
    alignIndicatorRows(dataList, series);
}

function indicatorCreate(
  series: IndicatorSeries,
  existingPaneId?: string,
): IndicatorCreate {
  const isSub = series.pane === "sub";
  return {
    id: `${INDICATOR_ID_PREFIX}${series.name}`,
    name: isSub ? STUDY_TEMPLATE : OVERLAY_TEMPLATE,
    shortName: series.name,
    series: isSub ? "normal" : "price",
    // 主图指标覆盖在 K 线窗格；副图沿用已有窗格，首次创建交给 KLineChart 新建。
    paneId: isSub ? existingPaneId : CANDLE_PANE_ID,
    figures: figuresFor(series),
    calc: calcFor(series),
  };
}

export interface OlderPage {
  bars: CompactBar[];
  hasMoreForward: boolean;
}

export interface ChartStats {
  loaded: number;
  gaps: number;
}

interface MarketChartProps {
  instrumentId: string;
  timeframe: string;
  pricePrecision: number;
  volumePrecision: number;
  theme: Theme;
  initialBars: CompactBar[];
  initialHasMoreForward: boolean;
  loadOlder: (beforeTs: number) => Promise<OlderPage>;
  /** 实时增量：同 timestamp 更新、新 timestamp 追加；FEED_GAP 不进 dataList。 */
  liveBars?: KlineEvent[];
  /** 后端 Python 算子产出的指标序列；只渲染，不在前端计算。 */
  indicatorSeries?: IndicatorSeries[];
  onStats: (stats: ChartStats) => void;
  onLoadError: (error: unknown) => void;
  /** 画线覆盖层：与 KLineChart 同尺寸的 SVG，父组件负责传入。 */
  overlay?: ReactNode;
  /** 图表实例挂载/销毁通知，供覆盖层做坐标投影。 */
  onChartReady?: (chart: Chart | null) => void;
}

/**
 * KLineChart v10 的薄封装。
 *
 * 数据约定（与 docs/architecture.md §3.1 一致）：
 * - API 返回升序紧凑柱；init 取最新 300 根；
 * - forward 用当前最老 timestamp 作为 before_ts 再取 300 根，prepend 到 dataList；
 * - backward 本单不实现，直接回调空数组并关闭该方向；
 * - FEED_GAP 在 toKLineSeries 中过滤，图表只负责可视化，状态栏单独展示断流数。
 */
export function MarketChart({
  instrumentId,
  timeframe,
  pricePrecision,
  volumePrecision,
  theme,
  initialBars,
  initialHasMoreForward,
  loadOlder,
  liveBars,
  indicatorSeries,
  onStats,
  onLoadError,
  overlay,
  onChartReady,
}: MarketChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const seriesRef = useRef<KLineData[]>([]);
  const gapsRef = useRef(0);
  const loadingOlderRef = useRef(false);
  const lastInitialTsRef = useRef(0);
  const barCallbackRef = useRef<((data: KLineData) => void) | null>(null);
  const pendingLiveRef = useRef<KlineEvent[]>([]);
  const deliveredRef = useRef<Map<number, number>>(new Map());
  const onChartReadyRef = useRef(onChartReady);
  onChartReadyRef.current = onChartReady;

  const flushLive = useCallback(() => {
    const callback = barCallbackRef.current;
    if (callback === null || pendingLiveRef.current.length === 0) {
      return;
    }
    const pending = pendingLiveRef.current;
    pendingLiveRef.current = [];
    for (const event of pending) {
      const delivered = deliveredRef.current.get(event.bar.timestamp);
      if (!shouldDeliverLiveBar(event, lastInitialTsRef.current, delivered)) {
        continue;
      }
      const point = toKLineData(event.bar);
      if (point === null) {
        // FEED_GAP 不能进入 dataList，否则会伪造价格。
        continue;
      }
      deliveredRef.current.set(event.bar.timestamp, event.revision);
      callback(point);
    }
  }, []);

  useEffect(() => {
    if (liveBars === undefined || liveBars.length === 0) {
      return;
    }
    for (const event of liveBars) {
      if (shouldDeliverLiveBar(event, lastInitialTsRef.current, undefined)) {
        pendingLiveRef.current.push(event);
      }
    }
    flushLive();
  }, [liveBars, flushLive]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) {
      return;
    }
    const chart = init(element, {
      locale: "zh-CN",
      timezone: "Asia/Shanghai",
    });
    if (!chart) {
      return;
    }
    chartRef.current = chart;
    onChartReadyRef.current?.(chart);
    chart.setStyles(theme);
    chart.setSymbol({
      ticker: instrumentId,
      pricePrecision,
      volumePrecision,
    });
    chart.setPeriod(toPeriod(timeframe));

    const initial = toKLineSeries(initialBars);
    seriesRef.current = initial.points;
    gapsRef.current = initial.gapCount;
    lastInitialTsRef.current =
      initial.points.length > 0
        ? initial.points[initial.points.length - 1].timestamp
        : 0;
    onStats({ loaded: initial.points.length, gaps: initial.gapCount });

    const dataLoader: DataLoader = {
      getBars: async ({ type, timestamp, callback }) => {
        if (type === "init") {
          callback(initial.points, {
            forward: initialHasMoreForward,
            backward: false,
          });
          return;
        }
        if (type !== "forward") {
          callback([], { forward: false, backward: false });
          return;
        }
        const oldest = timestamp ?? seriesRef.current[0]?.timestamp;
        if (oldest === undefined || loadingOlderRef.current) {
          return;
        }
        loadingOlderRef.current = true;
        try {
          const page = await loadOlder(oldest);
          const older = toKLineSeries(page.bars);
          seriesRef.current = [...older.points, ...seriesRef.current];
          gapsRef.current += older.gapCount;
          onStats({
            loaded: seriesRef.current.length,
            gaps: gapsRef.current,
          });
          callback(older.points, {
            forward: page.hasMoreForward,
            backward: false,
          });
        } catch (error) {
          onLoadError(error);
          callback([], { forward: false, backward: false });
        } finally {
          loadingOlderRef.current = false;
        }
      },
      subscribeBar: (params: DataLoaderSubscribeBarParams) => {
        barCallbackRef.current = params.callback;
        flushLive();
      },
      unsubscribeBar: (_params: DataLoaderUnsubscribeBarParams) => {
        barCallbackRef.current = null;
      },
    };
    chart.setDataLoader(dataLoader);

    const observer = new ResizeObserver(() => {
      chart.resize();
    });
    observer.observe(element);
    chart.resize();

    return () => {
      observer.disconnect();
      barCallbackRef.current = null;
      pendingLiveRef.current = [];
      deliveredRef.current.clear();
      onChartReadyRef.current?.(null);
      dispose(chart);
      chartRef.current = null;
    };
    // 快照切换时父组件用 key 重建图表；这里只读挂载时的初始数据。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.setStyles(theme);
  }, [theme]);

  useEffect(() => {
    const chart = chartRef.current;
    if (chart === null) {
      return;
    }
    const seriesList = indicatorSeries ?? [];
    const activeIds = new Set(
      seriesList.map((series) => `${INDICATOR_ID_PREFIX}${series.name}`),
    );
    for (const indicator of chart.getIndicators()) {
      if (
        indicator.id.startsWith(INDICATOR_ID_PREFIX) &&
        !activeIds.has(indicator.id)
      ) {
        chart.removeIndicator({ id: indicator.id });
      }
    }
    for (const series of seriesList) {
      const existing = chart.getIndicators({
        id: `${INDICATOR_ID_PREFIX}${series.name}`,
      });
      if (existing.length === 0) {
        // 主图指标共用 candle_pane：非 stack 创建会清空该窗格已有指标，
        // 因此主图必须 isStack=true；副图各自独立窗格，保持 false。
        chart.createIndicator(
          indicatorCreate(series),
          series.pane === "main",
        );
      } else {
        // 换新闭包，让 KLineChart 用最新后端序列重算；前端不保留旧值。
        // 副图沿用原 paneId，避免参数更新时重复创建窗格。
        chart.overrideIndicator(indicatorCreate(series, existing[0].paneId));
      }
    }
    const subPaneIds = new Set(
      chart
        .getIndicators()
        .filter(
          (indicator) =>
            indicator.id.startsWith(INDICATOR_ID_PREFIX) &&
            indicator.paneId !== CANDLE_PANE_ID,
        )
        .map((indicator) => indicator.paneId),
    );
    // 副图多于两个时压缩高度，保证主图仍有可读区域。
    const subHeight = subPaneIds.size >= 4 ? 88 : subPaneIds.size >= 3 ? 104 : 132;
    for (const paneId of subPaneIds) {
      chart.setPaneOptions({ id: paneId, height: subHeight });
    }
  }, [indicatorSeries]);

  return (
    <div className="chart-stage">
      <div className="market-chart" data-testid="market-chart" ref={containerRef} />
      {overlay}
    </div>
  );
}
