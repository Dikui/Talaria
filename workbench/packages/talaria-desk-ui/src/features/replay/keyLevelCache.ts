import type { KeyLevelSet } from "@talaria/contracts";

export type ReplayKeyLevelTrigger = "start" | "pause" | "step" | "seek" | "tick";

/**
 * Pause key-level recomputation during automated `play` so playback never saturates CPU.
 * Only recompute on `start`, `pause`, `step`, or manual `seek`.
 */
export function shouldFetchReplayKeyLevels(
  trigger: ReplayKeyLevelTrigger,
  isPlaying: boolean,
): boolean {
  if (isPlaying || trigger === "tick") return false;
  return trigger === "start" || trigger === "pause" || trigger === "step" || trigger === "seek";
}

export function keyLevelAsOfCacheKey(
  instrument: string,
  timeframe: string,
  asOf: number,
): string {
  return `${instrument}:${timeframe}:${asOf}`;
}

export class KeyLevelAsOfCache {
  private readonly maxEntries: number;
  private readonly entries = new Map<string, KeyLevelSet>();

  constructor(maxEntries = 128) {
    this.maxEntries = maxEntries;
  }

  get(instrument: string, timeframe: string, asOf: number): KeyLevelSet | undefined {
    const key = keyLevelAsOfCacheKey(instrument, timeframe, asOf);
    const hit = this.entries.get(key);
    if (!hit) return undefined;
    // Refresh insertion order for simple LRU eviction.
    this.entries.delete(key);
    this.entries.set(key, hit);
    return hit;
  }

  set(instrument: string, timeframe: string, asOf: number, value: KeyLevelSet): void {
    const key = keyLevelAsOfCacheKey(instrument, timeframe, asOf);
    if (this.entries.has(key)) {
      this.entries.delete(key);
    } else if (this.entries.size >= this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      if (oldest !== undefined) {
        this.entries.delete(oldest);
      }
    }
    this.entries.set(key, value);
  }

  has(instrument: string, timeframe: string, asOf: number): boolean {
    return this.entries.has(keyLevelAsOfCacheKey(instrument, timeframe, asOf));
  }

  clear(): void {
    this.entries.clear();
  }

  get size(): number {
    return this.entries.size;
  }
}
