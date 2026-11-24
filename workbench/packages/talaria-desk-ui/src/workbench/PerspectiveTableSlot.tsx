import type { ReactNode } from "react";

export const PERSPECTIVE_THRESHOLD_ROWS = 1000;

export interface PerspectiveTableSlotProps<T> {
  rows: readonly T[];
  tableName: "scanner" | "watchlist";
  fallback: ReactNode;
}

/**
 * Explicit adapter slot reserved for future FINOS Perspective (`@finos/perspective`, Apache-2.0 WASM)
 * high-row interactive pivot/grid rendering when watchlist or scanner row count exceeds ~1,000 rows.
 * Currently renders the lightweight React list/table `fallback` with zero WASM bundle overhead.
 */
export function PerspectiveTableSlot<T>({
  rows,
  tableName,
  fallback,
}: PerspectiveTableSlotProps<T>) {
  return (
    <div
      className="perspective-slot"
      data-perspective-slot={tableName}
      data-perspective-ready={rows.length >= PERSPECTIVE_THRESHOLD_ROWS ? "recommended" : "standby"}
    >
      {fallback}
    </div>
  );
}
