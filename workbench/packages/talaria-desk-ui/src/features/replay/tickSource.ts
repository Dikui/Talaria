import type { OHLCV, ReplayTick as VelaReplayTick, ReplayTickSource } from "@luxalgo/vela";
import type { ReplayTick as ContractReplayTick, TicksJsonResponse } from "@talaria/contracts";

export function contractTickToVelaTick(tick: ContractReplayTick): VelaReplayTick | null {
  const price = Number(tick.price);
  if (!Number.isFinite(price)) return null;
  const out: VelaReplayTick = { price };
  if (tick.open !== undefined && tick.open !== null) {
    const open = Number(tick.open);
    if (Number.isFinite(open)) out.open = open;
  }
  if (tick.high !== undefined && tick.high !== null) {
    const high = Number(tick.high);
    if (Number.isFinite(high)) out.high = high;
  }
  if (tick.low !== undefined && tick.low !== null) {
    const low = Number(tick.low);
    if (Number.isFinite(low)) out.low = low;
  }
  if (tick.volume !== undefined && tick.volume !== null) {
    const volume = Number(tick.volume);
    if (Number.isFinite(volume)) out.volume = volume;
  }
  return out;
}

export type FetchTicksFn = (
  options: {
    instrument: string;
    kind?: "trades" | "quotes";
    start: number;
    end: number;
    asOf?: number | null;
    limit?: number;
  },
  signal?: AbortSignal,
) => Promise<TicksJsonResponse>;

/**
 * Build a Vela `ReplayTickSource` backed by `GET /api/ticks`.
 * Empty arrays or failed/aborted requests return `[]` so Vela seamlessly falls
 * back to revealing the whole bar.
 */
export function createReplayTickSource(options: {
  instrumentId: string;
  asOf?: number | null;
  limit?: number;
  fetchTicks: FetchTicksFn;
}): ReplayTickSource {
  const limit = options.limit ?? 2000;
  return async (bar: OHLCV, { end, signal }) => {
    try {
      const response = await options.fetchTicks(
        {
          instrument: options.instrumentId,
          kind: "trades",
          start: bar.time,
          end,
          asOf: options.asOf,
          limit,
        },
        signal,
      );
      if (signal.aborted || !Array.isArray(response.ticks) || response.ticks.length === 0) {
        return [];
      }
      const mapped: VelaReplayTick[] = [];
      for (const item of response.ticks) {
        const converted = contractTickToVelaTick(item);
        if (converted !== null) mapped.push(converted);
      }
      return mapped;
    } catch {
      return [];
    }
  };
}
