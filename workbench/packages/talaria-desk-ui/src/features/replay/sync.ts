import type { LinkGroup } from "../../workbench/link";

export const REPLAY_SYNC_CHANNEL = "talaria-replay-sync-v1";

/**
 * Latest open timestamp for a bar of period `tfMs` to be strictly closed at `cursorTime`:
 * `bar.time + tf_ms <= cursorTime`  <=>  `bar.time <= cursorTime - tf_ms`.
 */
export function closedBarCutoff(cursorTime: number, tfMs: number): number {
  return cursorTime - tfMs;
}

export function isBarClosedAt(barOpenTime: number, tfMs: number, cursorTime: number): boolean {
  return barOpenTime + tfMs <= cursorTime;
}

/**
 * Strictly keep only closed bars satisfying `bar.time + tf_ms <= cursorTime`.
 */
export function filterClosedBars<T extends { time: number }>(
  bars: readonly T[],
  tfMs: number,
  cursorTime: number,
): T[] {
  return bars.filter((bar) => isBarClosedAt(bar.time, tfMs, cursorTime));
}

export interface ReplayPeerNext {
  paneId: string;
  tfMs: number;
  nextTime: number | null;
}

/**
 * Advance shared `cursorTime` by `Math.min(...nextTime)` across participating charts.
 * Each chart's `state.nextTime` is the open time of its next bar, whose close time is
 * `nextTime + tfMs`. When all peers are on the same bar close clock, this yields the
 * earliest bar completion boundary strictly ahead of `currentCursorTime` (if any).
 */
export function computeNextCursorTime(
  peers: readonly ReplayPeerNext[],
  currentCursorTime: number | null,
): number | null {
  const candidateCloses: number[] = [];
  for (const peer of peers) {
    if (peer.nextTime === null || !Number.isFinite(peer.nextTime)) continue;
    const closeTime = peer.nextTime + peer.tfMs;
    if (currentCursorTime === null || closeTime > currentCursorTime) {
      candidateCloses.push(closeTime);
    }
  }
  if (candidateCloses.length === 0) return null;
  return Math.min(...candidateCloses);
}

/**
 * Pure helper computing the earliest `nextTime` directly from raw `nextTime` values.
 */
export function minNextTime(nextTimes: ReadonlyArray<number | null | undefined>): number | null {
  let best = Infinity;
  for (const value of nextTimes) {
    if (typeof value === "number" && Number.isFinite(value) && value < best) {
      best = value;
    }
  }
  return Number.isFinite(best) ? best : null;
}

export type ReplaySyncMessage =
  | {
      type: "replay:sync-start";
      sessionId: string;
      masterPaneId: string;
      group: LinkGroup;
      instrumentId: string;
      cursorTime: number;
      showAllDrawings: boolean;
    }
  | {
      type: "replay:sync-cursor";
      sessionId: string;
      masterPaneId: string;
      group: LinkGroup;
      cursorTime: number;
    }
  | {
      type: "replay:sync-play";
      sessionId: string;
      masterPaneId: string;
      group: LinkGroup;
      playing: boolean;
      intervalMs: number;
    }
  | {
      type: "replay:sync-stop";
      sessionId: string;
      masterPaneId: string;
      group: LinkGroup;
    }
  | {
      type: "replay:sync-drawings";
      sessionId: string;
      masterPaneId: string;
      group: LinkGroup;
      showAllDrawings: boolean;
    }
  | {
      type: "replay:peer-next";
      sessionId: string;
      paneId: string;
      group: LinkGroup;
      tfMs: number;
      nextTime: number | null;
    };

export interface ReplaySyncTransport {
  send(message: ReplaySyncMessage): void;
  subscribe(listener: (message: ReplaySyncMessage) => void): () => void;
  close(): void;
}

export function openReplaySyncTransport(): ReplaySyncTransport {
  if (typeof BroadcastChannel === "undefined") {
    const listeners = new Set<(message: ReplaySyncMessage) => void>();
    return {
      send: (message) => {
        for (const listener of listeners) listener(message);
      },
      subscribe(listener) {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      close: () => listeners.clear(),
    };
  }
  const channel = new BroadcastChannel(REPLAY_SYNC_CHANNEL);
  return {
    send: (message) => channel.postMessage(message),
    subscribe(listener) {
      const receive = ({ data }: MessageEvent<ReplaySyncMessage>) => listener(data);
      channel.addEventListener("message", receive);
      return () => channel.removeEventListener("message", receive);
    },
    close: () => channel.close(),
  };
}
