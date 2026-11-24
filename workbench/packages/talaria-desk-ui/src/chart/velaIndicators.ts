import type { LineLikeKind } from "@luxalgo/vela";
import {
  registerNativeIndicator,
  stableSeriesId,
  type NativeIndicator,
  type NativeIndicatorContext,
} from "@luxalgo/vela/plugin";
import type { IndicatorName, IndicatorSeries } from "@talaria/contracts";

import { INDICATOR_COLORS } from "../features/indicators/config";
import { alignIndicatorChannelPoints, channelsFor, describeParams } from "../lib/indicatorData";

export const TALARIA_OVERLAY_INDICATOR_TYPE = "talaria:overlay";
export const TALARIA_STUDY_INDICATOR_TYPE = "talaria:study";

export interface AuthoritativeIndicatorSlot {
  series: IndicatorSeries;
  version: number;
}

const slotStore = new Map<string, AuthoritativeIndicatorSlot>();
let registered = false;

export function setAuthoritativeIndicatorSlot(
  slotKey: string,
  series: IndicatorSeries,
  version: number,
): void {
  slotStore.set(slotKey, { series, version });
}

export function clearAuthoritativeIndicatorSlot(slotKey: string): void {
  slotStore.delete(slotKey);
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

function emitSlot(ctx: NativeIndicatorContext, slotKey: string): void {
  const slot = slotStore.get(slotKey);
  if (!slot) {
    ctx.emit({ series: [] });
    return;
  }
  const { series } = slot;
  const bars = ctx.bars();
  const channels = channelsFor(series);
  const paneId = series.pane === "main" ? "price" : ctx.id;
  const overlay = series.pane === "main";
  const labelPrefix = describeParams(series);

  ctx.emit({
    series: channels.map((channel, ordinal) => ({
      id: stableSeriesId({
        instanceId: ctx.id,
        kind: lineKindFor(series.name),
        title: `${labelPrefix}.${channel}`,
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
  });
}

function createAuthoritativeNativeIndicator(): NativeIndicator {
  let ctx: NativeIndicatorContext | null = null;
  let slotKey = "";
  return {
    start(nextCtx, inputs) {
      ctx = nextCtx;
      slotKey = String(inputs.slotKey ?? "");
      emitSlot(nextCtx, slotKey);
    },
    onBars() {
      if (ctx) emitSlot(ctx, slotKey);
    },
    onViewport() {
      // No-op: backend authoritative series are bar-time aligned.
    },
    setInputs(inputs) {
      slotKey = String(inputs.slotKey ?? slotKey);
      if (ctx) emitSlot(ctx, slotKey);
    },
    suspend() {
      // Nothing to pause; host retains latest slot data.
    },
    resume() {
      if (ctx) emitSlot(ctx, slotKey);
    },
    stop() {
      ctx = null;
    },
  };
}

/**
 * Register Vela native indicators (`multiInstance: true`) that emit pre-computed
 * `talaria-data` IndicatorSeries directly without performing any JS math.
 */
export function ensureTalariaIndicatorsRegistered(): void {
  if (registered) return;
  registered = true;

  registerNativeIndicator({
    type: TALARIA_OVERLAY_INDICATOR_TYPE,
    title: "Talaria Overlay",
    paneHint: "price",
    overlay: true,
    multiInstance: true,
    legend: true,
    inputsSchema: () => [
      { key: "slotKey", title: "Slot", type: "string", defval: "" },
      { key: "version", title: "Version", type: "int", defval: 0 },
    ],
    defaultInputs: () => ({ slotKey: "", version: 0 }),
    create: createAuthoritativeNativeIndicator,
  });

  registerNativeIndicator({
    type: TALARIA_STUDY_INDICATOR_TYPE,
    title: "Talaria Study",
    paneHint: "new",
    overlay: false,
    multiInstance: true,
    legend: true,
    inputsSchema: () => [
      { key: "slotKey", title: "Slot", type: "string", defval: "" },
      { key: "version", title: "Version", type: "int", defval: 0 },
    ],
    defaultInputs: () => ({ slotKey: "", version: 0 }),
    create: createAuthoritativeNativeIndicator,
  });
}
