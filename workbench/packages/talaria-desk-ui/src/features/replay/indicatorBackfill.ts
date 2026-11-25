import type { IndicatorSeries } from "@talaria/contracts";

export function oldestIndicatorTimestamp(
  seriesList: readonly IndicatorSeries[],
): number | null {
  let oldest = Infinity;
  for (const series of seriesList) {
    if (!Array.isArray(series.points)) continue;
    for (const point of series.points) {
      if (point.timestamp < oldest) {
        oldest = point.timestamp;
      }
    }
  }
  return Number.isFinite(oldest) ? oldest : null;
}

/**
 * True when replay `start({ from })` deepened chart history earlier than the cached
 * `IndicatorSeries` start (`bars[0].time < oldestIndicatorTs`).
 */
export function needsIndicatorDeepenRefetch(
  oldestBarTime: number | null,
  seriesList: readonly IndicatorSeries[],
): boolean {
  if (oldestBarTime === null || seriesList.length === 0) return false;
  const oldestInd = oldestIndicatorTimestamp(seriesList);
  if (oldestInd === null) return false;
  return oldestBarTime < oldestInd;
}

export function computeDeepenIndicatorLimit(
  oldestBarTime: number,
  newestBarTime: number,
  tfMs: number,
  minLimit = 300,
  maxLimit = 5000,
): number {
  if (tfMs <= 0 || newestBarTime <= oldestBarTime) return minLimit;
  const estimated = Math.ceil((newestBarTime - oldestBarTime) / tfMs) + 16;
  return Math.max(minLimit, Math.min(maxLimit, estimated));
}
