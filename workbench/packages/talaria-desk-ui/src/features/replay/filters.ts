import type { AgentAnnotationRecord } from "@talaria/contracts";

import type { ReplayDrawingFilter } from "../../chart/adapter";

export function filterReplayAnnotations(
  annotations: readonly AgentAnnotationRecord[],
  filter: ReplayDrawingFilter,
): AgentAnnotationRecord[] {
  if (!filter.active || filter.showAll || filter.cursorTime === null) {
    return [...annotations];
  }
  return annotations.filter((item) => item.created_ts <= filter.cursorTime!);
}
