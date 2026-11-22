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
  RulePreviewResponse,
} from "@talaria/contracts";

import { toKLineData, toKLineSeries, toPeriod } from "../../lib/chartData";
import { alignIndicatorRows } from "../../lib/indicatorData";
import {
  INDICATOR_CHANNELS,
  INDICATOR_COLORS,
} from "../indicators/config";
import type { Theme } from "../../lib/theme";
import type { PanelLink } from "../../workbench/link";

import { CANDLE_PANE_ID, observeLinkedCrosshair, observeLocalCrosshair } from "./crosshair";
import { shouldDeliverLiveBar } from "./liveDelivery";
import { OVERLAY_TEMPLATE, STUDY_TEMPLATE, ruleChartIndicators } from "../rules/ruleChart";

type IndicatorRow = Record<string, number | null>;

const INDICATOR_ID_PREFIX = "talaria-";

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
): IndicatorCreate {
  const isSub = series.pane === "sub";
  return {
    id: `${INDICATOR_ID_PREFIX}${series.name}`,
    name: isSub ? STUDY_TEMPLATE : OVERLAY_TEMPLATE,
    shortName: series.name,
    series: isSub ? "normal" : "price",
    // 主图指标覆盖在 K 线窗格；副图沿用已有窗格，首次创建交给 KLineChart 新建。
    paneId: isSub ? undefined : CANDLE_PANE_ID,
    figures: figuresFor(series),
    calc: calcFor(series),
  };
}

function fitIndicatorPanes(chart: Chart, chartHeight: number) {
  const subPaneIds = new Set(
    chart.getIndicators()
      .filter((indicator) =>
        indicator.id.startsWith(INDICATOR_ID_PREFIX) && indicator.paneId !== CANDLE_PANE_ID,
      )
      .map((indicator) => indicator.paneId),
  );
  const count = subPaneIds.size;
  if (count === 0) return;

  const preferredHeight = count >= 4 ? 88 : count >= 3 ? 104 : 132;
  const axisHeight = chart.getSize("x_axis_pane")?.height ?? 0;
  const plotHeight = chartHeight - axisHeight - count * chart.getStyles().separator.size;
  // 图表上方的面板展开时画布至少 480px，优先给主图留出 320px。
  const candleHeight = Math.min(320, Math.floor(plotHeight * 0.75));
  const heightPerPane = Math.floor((plotHeight - candleHeight) / count);
  const subHeight = Math.max(1, Math.min(preferredHeight, heightPerPane));
  for (const id of subPaneIds) {
    chart.setPaneOptions({ id, height: subHeight, minHeight: Math.min(30, subHeight) });
  }
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
  rulePreview?: RulePreviewResponse | null;
  onStats: (stats: ChartStats) => void;
  onLoadError: (error: unknown) => void;
  /** 画线覆盖层：与 KLineChart 同尺寸的 SVG，父组件负责传入。 */
  overlay?: ReactNode;
  /** 图表实例挂载/销毁通知，供覆盖层做坐标投影。 */
  onChartReady?: (chart: Chart | null) => void;
  locateTs?: number | null;
  crosshairLink?: Pick<PanelLink, "getCrosshair" | "subscribeCrosshair" | "sendCrosshair">;
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
  rulePreview,
  onStats,
  onLoadError,
  overlay,
  onChartReady,
  locateTs = null,
  crosshairLink,
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
  const cursorRef = useRef<ReturnType<typeof observeLinkedCrosshair> | null>(null);
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
    cursorRef.current?.refresh();
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
          cursorRef.current?.refresh();
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
          cursorRef.current?.refresh();
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

    let chartHeight = element.clientHeight;
    const observer = new ResizeObserver(() => {
      const height = element.clientHeight;
      if (height !== chartHeight) {
        fitIndicatorPanes(chart, height);
        chartHeight = height;
      }
      chart.resize();
      cursorRef.current?.refresh();
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
    if (locateTs !== null && chartRef.current?.getDataList().length) {
      chartRef.current.scrollToTimestamp(locateTs);
    }
  }, [locateTs]);

  useEffect(() => {
    const chart = chartRef.current;
    const element = containerRef.current;
    if (!chart || !element || !crosshairLink) return;
    const cursor = observeLinkedCrosshair(chart, crosshairLink.getCrosshair);
    cursorRef.current = cursor;
    const stopRemote = crosshairLink.subscribeCrosshair(cursor.refresh);
    const stopLocal = observeLocalCrosshair(chart, element, crosshairLink.sendCrosshair);
    cursor.refresh();
    return () => {
      cursorRef.current = null;
      stopLocal();
      stopRemote();
      cursor.close();
    };
  }, [crosshairLink]);

  useEffect(() => {
    chartRef.current?.setStyles(theme);
  }, [theme]);

  useEffect(() => {
    const chart = chartRef.current;
    const element = containerRef.current;
    if (!chart || !element) {
      return;
    }
    const markerColor = getComputedStyle(element).getPropertyValue("--accent").trim();
    const configList = [
      ...(indicatorSeries ?? []).map(indicatorCreate),
      ...ruleChartIndicators(rulePreview, markerColor),
    ];
    const activeIds = new Set(configList.map(({ id }) => id));
    for (const indicator of chart.getIndicators()) {
      if (
        indicator.id.startsWith(INDICATOR_ID_PREFIX) &&
        !activeIds.has(indicator.id)
      ) {
        chart.removeIndicator({ id: indicator.id });
      }
    }
    for (const config of configList) {
      const existing = chart.getIndicators({ id: config.id });
      if (existing.length === 0) {
        // 主图指标共用 candle_pane：非 stack 创建会清空该窗格已有指标，
        // 因此主图必须 isStack=true；副图各自独立窗格，保持 false。
        chart.createIndicator(config, config.paneId === CANDLE_PANE_ID);
      } else {
        // 换新闭包，让 KLineChart 用最新后端序列重算；前端不保留旧值。
        // 副图沿用原 paneId，避免参数更新时重复创建窗格。
        chart.overrideIndicator({ ...config, paneId: existing[0].paneId });
      }
    }
    fitIndicatorPanes(chart, element.clientHeight);
  }, [indicatorSeries, rulePreview, theme]);

  return (
    <div className="chart-stage">
      <div className="market-chart" data-testid="market-chart" ref={containerRef} />
      {overlay}
    </div>
  );
}
