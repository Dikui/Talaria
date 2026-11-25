import type {
  ExecutionHandlers,
  ExecutionRequest,
  ExecutionSession,
  IndicatorModel,
  LineLikeKind,
  OHLCV,
  PreparedScript,
  ScriptingEngine,
  TradeExecution,
} from "@luxalgo/vela";
import { stableSeriesId } from "@luxalgo/vela/plugin";
import type { IndicatorName, IndicatorSeries } from "@talaria/contracts";

import { INDICATOR_COLORS } from "../features/indicators/config";
import { getLocale, type Locale } from "../lib/i18n";
import { alignIndicatorChannelPoints, channelsFor, describeParams } from "../lib/indicatorData";

export const TALARIA_INDICATOR_LANGUAGE = "talaria-indicator";
export const TALARIA_TRADE_MARKERS_TYPE = "talaria:trade_markers";
export const TALARIA_TRADE_MARKERS_LANGUAGE = "talaria-trades";

export interface AuthoritativeIndicatorSlot {
  series: IndicatorSeries;
  version: number;
  locale: Locale;
}

export interface TradeMarkersSlot {
  trades: TradeExecution[];
  version: number;
}

const slotStore = new Map<string, AuthoritativeIndicatorSlot>();
const tradeMarkerStore = new Map<string, TradeMarkersSlot>();

export function setAuthoritativeIndicatorSlot(
  slotKey: string,
  series: IndicatorSeries,
  version: number,
  locale: Locale = getLocale(),
): void {
  slotStore.set(slotKey, { series, version, locale });
}

export function setAuthoritativeIndicatorLocale(slotKey: string, locale: Locale): boolean {
  const existing = slotStore.get(slotKey);
  if (!existing) return false;
  slotStore.set(slotKey, { ...existing, locale });
  return true;
}

export function clearAuthoritativeIndicatorSlot(slotKey: string): void {
  slotStore.delete(slotKey);
}

export function setChartTradeMarkers(
  chartId: string,
  trades: TradeExecution[],
  version: number,
): void {
  tradeMarkerStore.set(chartId, { trades, version });
}

export function clearChartTradeMarkers(chartId: string): void {
  tradeMarkerStore.delete(chartId);
}

/**
 * Filter TradeExecution markers to bars currently visible on the chart (so replay
 * never leaks future fills while preserving intra-bar fill order for stacking).
 */
export function selectVisibleTradeMarkers(
  trades: readonly TradeExecution[],
  bars: readonly Pick<OHLCV, "time">[],
): TradeExecution[] {
  if (trades.length === 0 || bars.length === 0) return [];
  const firstTime = bars[0].time;
  const lastTime = bars[bars.length - 1].time;
  return trades.filter((trade) => trade.time >= firstTime && trade.time <= lastTime);
}

export function channelColor(name: IndicatorName, channel: string): string {
  if (name === "BOLL") {
    if (channel === "upper") return "#2563eb";
    if (channel === "lower") return "#db2777";
    return "#7c3aed";
  }
  return INDICATOR_COLORS[name] ?? "#2563eb";
}

function lineKindFor(name: IndicatorName): LineLikeKind {
  if (name === "NET_ODDS") return "histogram";
  return "line";
}

function buildAuthoritativeIndicatorModel(
  instanceId: string,
  slotKey: string,
  bars: readonly OHLCV[],
  inputs: Record<string, string | number | boolean>,
): IndicatorModel {
  const slot = slotStore.get(slotKey);
  if (!slot) {
    return {
      id: instanceId,
      title: slotKey,
      overlay: false,
      paneHint: "new",
      paneId: instanceId,
      series: [],
      fills: [],
      backgrounds: [],
      priceLines: [],
      inputs: [],
      inputValues: inputs,
    };
  }
  const { series, locale } = slot;
  const channels = channelsFor(series);
  const overlay = series.pane === "main";
  const paneHint = overlay ? "price" : "new";
  const paneId = overlay ? "price" : instanceId;
  const labelPrefix = describeParams(series, locale);

  return {
    id: instanceId,
    title: labelPrefix,
    shorttitle: labelPrefix,
    overlay,
    paneHint,
    paneId,
    ...(bars.length > 0 ? { anchorTime: bars[0].time } : {}),
    series: channels.map((channel, ordinal) => ({
      id: stableSeriesId({
        instanceId,
        kind: lineKindFor(series.name),
        title: `${series.name}.${channel}`,
        ordinal,
      }),
      title: channels.length === 1 ? labelPrefix : `${labelPrefix} ${channel}`,
      paneId,
      overlay,
      kind: lineKindFor(series.name),
      points: alignIndicatorChannelPoints(bars, series, channel),
      style: {
        color: channelColor(series.name, channel),
        width: channel === "mid" ? 1 : 2,
        lineStyle: channel === "upper" || channel === "lower" ? "dashed" : "solid",
      },
    })),
    fills: [],
    backgrounds: [],
    priceLines: [],
    inputs: [],
    inputValues: inputs,
  };
}

/**
 * Lightweight ScriptingEngine backed by `@luxalgo/vela` that emits pre-computed
 * `talaria-data` IndicatorSeries with per-indicator titles/shorttitles and zero JS math.
 */
export function createAuthoritativeIndicatorEngine(): ScriptingEngine {
  return {
    language: TALARIA_INDICATOR_LANGUAGE,
    capabilities: {
      streaming: true,
      visibleRange: false,
      inputs: true,
      props: false,
    },
    async prepare(source: string, instanceId: string): Promise<PreparedScript> {
      const slot = slotStore.get(source);
      return {
        language: TALARIA_INDICATOR_LANGUAGE,
        inputs: [],
        meta: {
          title: slot ? describeParams(slot.series, slot.locale) : source,
          overlay: slot ? slot.series.pane === "main" : false,
        },
        reactsToViewport: false,
        token: instanceId,
      };
    },
    execute(req: ExecutionRequest, handlers: ExecutionHandlers): ExecutionSession {
      let currentInputs = { ...req.inputs };
      const instanceId = String(req.prepared.token ?? "talaria:indicator");

      const emitCurrent = () => {
        const slotKey = String(currentInputs.slotKey ?? "");
        const bars = req.getBars ? req.getBars() : req.bars;
        handlers.onModel(
          buildAuthoritativeIndicatorModel(instanceId, slotKey, bars, currentInputs),
        );
      };

      emitCurrent();

      return {
        stop() {},
        update(inputs) {
          currentInputs = { ...currentInputs, ...inputs };
          emitCurrent();
        },
        setVisibleRange() {},
        notifyBars() {
          emitCurrent();
        },
      };
    },
  };
}

function buildTradeMarkersModel(
  instanceId: string,
  chartId: string,
  bars: readonly OHLCV[],
  inputs: Record<string, string | number | boolean>,
): IndicatorModel {
  const slot = tradeMarkerStore.get(chartId);
  const trades =
    slot && slot.trades.length > 0 ? selectVisibleTradeMarkers(slot.trades, bars) : [];
  return {
    id: instanceId,
    title: "Talaria Trade Markers",
    overlay: true,
    paneHint: "price",
    paneId: "price",
    legend: false,
    series: [],
    fills: [],
    backgrounds: [],
    priceLines: [],
    trades,
    inputs: [
      { key: "chartId", title: "Chart ID", type: "string", defval: "" },
      { key: "version", title: "Version", type: "int", defval: 0 },
    ],
    inputValues: inputs,
  };
}

/**
 * Lightweight ScriptingEngine backed by `@luxalgo/vela` (no external scripting runtime)
 * that emits `TradeExecution[]` directly into Vela's native `renderTradeMarkers` pipeline.
 */
export function createTradeMarkersEngine(): ScriptingEngine {
  return {
    language: TALARIA_TRADE_MARKERS_LANGUAGE,
    capabilities: {
      streaming: true,
      visibleRange: false,
      inputs: true,
      props: false,
    },
    async prepare(_source: string, instanceId: string): Promise<PreparedScript> {
      return {
        language: TALARIA_TRADE_MARKERS_LANGUAGE,
        inputs: [
          { key: "chartId", title: "Chart ID", type: "string", defval: "" },
          { key: "version", title: "Version", type: "int", defval: 0 },
        ],
        meta: {
          title: "Talaria Trade Markers",
          overlay: true,
        },
        reactsToViewport: false,
        token: instanceId,
      };
    },
    execute(req: ExecutionRequest, handlers: ExecutionHandlers): ExecutionSession {
      let currentInputs = { ...req.inputs };
      const instanceId = String(req.prepared.token ?? "talaria:trade_markers");

      const emitCurrent = () => {
        const chartId = String(currentInputs.chartId ?? "");
        const bars = req.getBars ? req.getBars() : req.bars;
        handlers.onModel(buildTradeMarkersModel(instanceId, chartId, bars, currentInputs));
      };

      emitCurrent();

      return {
        stop() {},
        update(inputs) {
          currentInputs = { ...currentInputs, ...inputs };
          emitCurrent();
        },
        setVisibleRange() {},
        notifyBars() {
          emitCurrent();
        },
      };
    },
  };
}