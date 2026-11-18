import { useCallback, useEffect, useRef } from "react";
import {
  dispose,
  init,
  type Chart,
  type DataLoader,
  type DataLoaderSubscribeBarParams,
  type DataLoaderUnsubscribeBarParams,
  type KLineData,
} from "klinecharts";

import type { CompactBar, KlineEvent } from "@talaria/contracts";

import { toKLineData, toKLineSeries, toPeriod } from "../../lib/chartData";
import type { Theme } from "../../lib/theme";

import { shouldDeliverLiveBar } from "./liveDelivery";

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
  onStats: (stats: ChartStats) => void;
  onLoadError: (error: unknown) => void;
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
  onStats,
  onLoadError,
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
      dispose(chart);
      chartRef.current = null;
    };
    // 快照切换时父组件用 key 重建图表；这里只读挂载时的初始数据。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.setStyles(theme);
  }, [theme]);

  return <div className="market-chart" data-testid="market-chart" ref={containerRef} />;
}
