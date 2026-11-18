import type { KlineEvent } from "@talaria/contracts";

/**
 * 判断一条实时 K 线是否要投递给图表。
 *
 * - HISTORICAL_PIT 快照的最后一根通常就是“当前进行中”的 K 线，因此实时事件
 *   允许与最后一根历史柱 timestamp 相等（同一根柱的修订要继续投递）；
 * - 比快照最后一根更旧的事件属于历史，丢弃；
 * - 同一 timestamp 已经投递过相同或更高的 revision，说明是重复推送，丢弃。
 */
export function shouldDeliverLiveBar(
  event: KlineEvent,
  lastInitialTs: number,
  deliveredRevision: number | undefined,
): boolean {
  if (event.bar.timestamp < lastInitialTs) {
    return false;
  }
  if (deliveredRevision !== undefined && deliveredRevision >= event.revision) {
    return false;
  }
  return true;
}
