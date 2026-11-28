import type {
  DrawingTypeKey,
  DrawingsDocument,
  SerializedDrawing,
  Vela,
} from "@luxalgo/vela";
import { registerIcon } from "@luxalgo/vela";
import { drawingActions, registerDrawingAction } from "@luxalgo/vela/plugin";
import type {
  DrawingCreate,
  DrawingKind,
  DrawingPatch,
  DrawingPoint,
  DrawingRecord,
  DrawingStyle,
} from "@talaria/contracts";

import { api } from "../lib/api";
import { t } from "../lib/i18n";
import type { DrawingSyncSource, ReplayDrawingFilter } from "./adapter";
import { createSerialQueue } from "./writeQueue";

export function filterReplayDrawings(
  records: readonly DrawingRecord[],
  filter: ReplayDrawingFilter,
): DrawingRecord[] {
  return records.filter((record) => {
    if (record.deleted_at != null) return false;
    if (!filter.active || filter.showAll || filter.cursorTime === null) {
      return true;
    }
    return record.created_ts <= filter.cursorTime;
  });
}

const VELA_EXTENSION_PREFIX = "[talaria-vela:v1]";
export const ALERT_ACTION_ID = "talaria.create_price_alert";
const ALERT_ICON_ID = "talaria-alert-bell";
export const DRAFT_ACTION_ID = "talaria.create_paper_draft";
const DRAFT_ICON_ID = "talaria-order-draft";

export function isEphemeralOrderDrawing(drawing: Pick<SerializedDrawing, "type">): boolean {
  return drawing.type === "position";
}

let actionRegistered = false;
const activeAlertHandlers = new WeakMap<
  object,
  (payload: { drawingId: string; price: number | null; type: string }) => void
>();
const activeDraftHandlers = new WeakMap<object, (drawing: SerializedDrawing) => void>();

export function ensureDrawingAlertActionRegistered(): void {
  if (actionRegistered) return;
  actionRegistered = true;

  registerIcon(
    ALERT_ICON_ID,
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
  );
  registerIcon(
    DRAFT_ICON_ID,
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><line x1="9" y1="15" x2="15" y2="15"/></svg>',
  );

  registerDrawingAction({
    id: DRAFT_ACTION_ID,
    icon: DRAFT_ICON_ID,
    label: t("生成交易草案", "Create Draft"),
    order: -1,
    surfaces: ["toolbar", "settings"],
    when: (drawing) => drawing.type === "position" && drawing.anchors.length >= 3,
    run: (ctx, drawing) => {
      const handler = activeDraftHandlers.get(ctx.chart);
      handler?.(drawing);
    },
  });

  registerDrawingAction({
    id: ALERT_ACTION_ID,
    icon: ALERT_ICON_ID,
    label: t("创建价格预警", "Create price alert"),
    order: 0,
    surfaces: ["toolbar", "settings"],
    when: (drawing) => drawing.type !== "position" && drawing.anchors.length > 0,
    run: (ctx, drawing) => {
      const handler = activeAlertHandlers.get(ctx.chart);
      const price = drawing.anchors[0]?.price ?? null;
      handler?.({ drawingId: drawing.id, price, type: drawing.type });
    },
  });
}

export interface ChartDrawingActionHandlers {
  onCreateAlert?: (payload: { drawingId: string; price: number | null; type: string }) => void;
  onCreateDraft?: (drawing: SerializedDrawing) => void;
}

/**
 * Unified `chart.renderer.setDrawingActions` dispatcher hosting both price-alert
 * actions (on HUMAN_DRAWING) and "Create Draft" actions (on ephemeral `position`).
 */
export function bindChartDrawingActions(
  chart: Vela,
  handlers: ChartDrawingActionHandlers,
): () => void {
  ensureDrawingAlertActionRegistered();
  if (handlers.onCreateAlert) {
    activeAlertHandlers.set(chart, handlers.onCreateAlert);
  } else {
    activeAlertHandlers.delete(chart);
  }
  if (handlers.onCreateDraft) {
    activeDraftHandlers.set(chart, handlers.onCreateDraft);
  } else {
    activeDraftHandlers.delete(chart);
  }

  chart.renderer.setDrawingActions((drawingId: string) => {
    const drawing = chart.drawings.all().find((item) => item.id === drawingId);
    if (!drawing) return [];
    return drawingActions()
      .filter((desc) => {
        if (desc.id === DRAFT_ACTION_ID && !handlers.onCreateDraft) return false;
        if (desc.id === ALERT_ACTION_ID && !handlers.onCreateAlert) return false;
        return !desc.when || desc.when(drawing);
      })
      .map((desc) => {
        const surfaces = desc.surfaces ?? ["toolbar", "settings"];
        const resolvedLabel =
          desc.id === DRAFT_ACTION_ID
            ? t("生成交易草案", "Create Draft")
            : desc.id === ALERT_ACTION_ID
              ? t("创建价格预警", "Create price alert")
              : typeof desc.label === "string"
                ? desc.label
                : desc.id;
        return {
          id: desc.id,
          icon: desc.icon,
          label: resolvedLabel,
          toolbar: surfaces.includes("toolbar"),
          settings: surfaces.includes("settings"),
          run: () => {
            const latest = chart.drawings.all().find((item) => item.id === drawingId) ?? drawing;
            if (desc.id === DRAFT_ACTION_ID) {
              handlers.onCreateDraft?.(latest);
            } else if (desc.id === ALERT_ACTION_ID) {
              const price = latest.anchors[0]?.price ?? null;
              handlers.onCreateAlert?.({
                drawingId: latest.id,
                price,
                type: latest.type,
              });
            }
          },
        };
      });
  });

  return () => {
    activeAlertHandlers.delete(chart);
    activeDraftHandlers.delete(chart);
    chart.renderer.setDrawingActions(null);
  };
}

export function bindChartDrawingAlertHandler(
  chart: Vela,
  handler: (payload: { drawingId: string; price: number | null; type: string }) => void,
): () => void {
  return bindChartDrawingActions(chart, { onCreateAlert: handler });
}

const VELA_TO_CONTRACT_KIND: Partial<Record<DrawingTypeKey, DrawingKind>> = {
  hline: "HORIZONTAL_LINE",
  ray: "RAY",
  trendline: "SEGMENT",
  parallelchannel: "PARALLEL_CHANNEL",
  fibretracement: "FIB_RETRACEMENT",
  box: "RECTANGLE",
  text: "TEXT_LABEL",
};

const CONTRACT_TO_VELA_KIND: Record<DrawingKind, DrawingTypeKey> = {
  HORIZONTAL_LINE: "hline",
  RAY: "ray",
  SEGMENT: "trendline",
  PARALLEL_CHANNEL: "parallelchannel",
  FIB_RETRACEMENT: "fibretracement",
  RECTANGLE: "box",
  TEXT_LABEL: "text",
};

function formatPrice(price: number, precision: number): string {
  const digits = Math.min(8, Math.max(0, Math.trunc(precision)));
  return price.toFixed(digits);
}

function toContractStyle(style: SerializedDrawing["style"]): DrawingStyle {
  return {
    color: style.lineColor || "#4c8dff",
    line_width: Math.max(1, Math.min(6, Math.round(style.lineWidth || 1))),
    line_style: style.lineStyle ?? "solid",
    fill_opacity: style.fillOpacity ?? 0.12,
  };
}

function toVelaLineStyle(style?: DrawingStyle): SerializedDrawing["style"] {
  const color = style?.color ?? "#4c8dff";
  return {
    lineColor: color,
    lineWidth: style?.line_width ?? 1,
    lineStyle: style?.line_style ?? "solid",
    fillColor: color,
    fillOpacity: style?.fill_opacity ?? 0.12,
  };
}

function toVelaText(text?: string): SerializedDrawing["text"] {
  if (!text) return undefined;
  return {
    value: text,
    size: "normal",
    hAlign: "center",
    vAlign: "center",
  };
}

export function serializedDrawingToCreate(
  drawing: SerializedDrawing,
  instrumentId: string,
  timeframe: string,
  pricePrecision: number,
): DrawingCreate {
  const mappedKind = VELA_TO_CONTRACT_KIND[drawing.type];
  const points: DrawingPoint[] = drawing.anchors.map((anchor) => ({
    ts: Math.round(anchor.time),
    price: formatPrice(anchor.price, pricePrecision),
  }));

  if (mappedKind !== undefined) {
    return {
      instrument_id: instrumentId,
      kind: mappedKind,
      visible_timeframes: drawing.showOn ?? (timeframe ? [timeframe] : []),
      points,
      text: drawing.text?.value ?? "",
      style: toContractStyle(drawing.style),
    };
  }

  // Encode any of the remaining 72 Vela drawing tools safely into a TEXT_LABEL envelope.
  return {
    instrument_id: instrumentId,
    kind: "TEXT_LABEL",
    visible_timeframes: drawing.showOn ?? (timeframe ? [timeframe] : []),
    points: points.slice(0, 1),
    text: `${VELA_EXTENSION_PREFIX}${JSON.stringify({
      type: drawing.type,
      anchors: drawing.anchors,
      style: drawing.style,
      text: drawing.text,
      props: drawing.props,
    })}`,
    style: toContractStyle(drawing.style),
  };
}

export function contractDrawingToSerialized(record: DrawingRecord): SerializedDrawing {
  if (record.kind === "TEXT_LABEL" && record.text.startsWith(VELA_EXTENSION_PREFIX)) {
    try {
      const payload = JSON.parse(record.text.slice(VELA_EXTENSION_PREFIX.length)) as {
        type: DrawingTypeKey;
        anchors: SerializedDrawing["anchors"];
        style?: SerializedDrawing["style"];
        text?: SerializedDrawing["text"];
        props?: Record<string, unknown>;
      };
      return {
        id: record.drawing_id,
        type: payload.type,
        paneId: "price",
        anchors: payload.anchors,
        style: payload.style ?? toVelaLineStyle(record.style),
        text: payload.text,
        locked: false,
        visible: true,
        zIndex: 1,
        createdAt: record.created_ts,
        props: payload.props,
        showOn: record.visible_timeframes.length > 0 ? record.visible_timeframes : undefined,
      };
    } catch {
      // Fall back to standard TEXT_LABEL below.
    }
  }

  return {
    id: record.drawing_id,
    type: CONTRACT_TO_VELA_KIND[record.kind],
    paneId: "price",
    anchors: record.points.map((pt) => ({ time: pt.ts, price: Number(pt.price) })),
    style: toVelaLineStyle(record.style),
    text: toVelaText(record.text),
    locked: false,
    visible: true,
    zIndex: 1,
    createdAt: record.created_ts,
    showOn: record.visible_timeframes.length > 0 ? record.visible_timeframes : undefined,
  };
}

export interface DrawingPersistenceClient {
  drawings(
    instrumentId: string,
    timeframe: string,
    opts?: { all_timeframes?: boolean },
  ): Promise<{ drawings: DrawingRecord[] }>;
  createDrawing(payload: DrawingCreate): Promise<DrawingRecord>;
  patchDrawing(drawingId: string, patch: DrawingPatch): Promise<DrawingRecord>;
  deleteDrawing(drawingId: string): Promise<DrawingRecord>;
}

/**
 * Synchronizes Vela `chart.drawings` (`toJSON`/`fromJSON` + events) with `/api/drawings*`
 * using debounced serial writes (`writeQueue`) to preserve drawing version history.
 */
export class VelaDrawingSyncBridge {
  private readonly chart: Vela;
  private readonly client: DrawingPersistenceClient;
  private readonly queue = createSerialQueue();
  private instrumentId: string;
  private timeframe: string;
  private pricePrecision: number;
  private localToRemoteId = new Map<string, string>();
  private remoteRecords = new Map<string, DrawingRecord>();
  private ephemeralIds = new Set<string>();
  private debounceTimers = new Map<string, number>();
  private selectedRemoteId: string | null = null;
  private suppressEvents = false;
  private readOnly = false;
  private replayFilter: ReplayDrawingFilter = {
    active: false,
    cursorTime: null,
    showAll: false,
  };
  private unsubscribers: Array<() => void> = [];
  private onChanged?: (records: DrawingRecord[], source: DrawingSyncSource) => void;
  private onSelected?: (drawingId: string | null) => void;
  private onError?: (message: string) => void;

  constructor(options: {
    chart: Vela;
    instrumentId: string;
    timeframe: string;
    pricePrecision: number;
    readOnly?: boolean;
    client?: DrawingPersistenceClient;
    onChanged?: (records: DrawingRecord[], source: DrawingSyncSource) => void;
    onSelected?: (drawingId: string | null) => void;
    onError?: (message: string) => void;
  }) {
    this.chart = options.chart;
    this.instrumentId = options.instrumentId;
    this.timeframe = options.timeframe;
    this.pricePrecision = options.pricePrecision;
    this.readOnly = options.readOnly ?? false;
    this.client = options.client ?? api;
    this.onChanged = options.onChanged;
    this.onSelected = options.onSelected;
    this.onError = options.onError;

    this.attachEvents();
  }

  private isEffectiveReadOnly(): boolean {
    return this.readOnly || this.replayFilter.active;
  }

  private applyInteractiveState(): void {
    const effective = this.isEffectiveReadOnly();
    this.chart.renderer.set("drawingsInteractive", !effective);
    this.chart.drawings.showToolbar(!effective);
  }

  private attachEvents(): void {
    this.unsubscribers.push(
      this.chart.on("drawing:created", ({ id }) => {
        if (this.suppressEvents || this.isEffectiveReadOnly()) return;
        const drawing = this.chart.drawings.all().find((d) => d.id === id);
        if (drawing && isEphemeralOrderDrawing(drawing)) {
          this.ephemeralIds.add(id);
          return;
        }
        this.scheduleCreate(id);
      }),
      this.chart.on("drawing:edited", ({ id }) => {
        if (this.suppressEvents || this.isEffectiveReadOnly()) return;
        if (this.ephemeralIds.has(id)) return;
        const drawing = this.chart.drawings.all().find((d) => d.id === id);
        if (drawing && isEphemeralOrderDrawing(drawing)) {
          this.ephemeralIds.add(id);
          return;
        }
        this.scheduleEdit(id);
      }),
      this.chart.on("drawing:removed", ({ id }) => {
        if (this.ephemeralIds.has(id)) {
          this.ephemeralIds.delete(id);
          return;
        }
        if (this.suppressEvents || this.isEffectiveReadOnly()) return;
        this.scheduleDelete(id);
      }),
      this.chart.on("drawing:history", () => {
        if (this.suppressEvents || this.isEffectiveReadOnly()) return;
        // Defensive guard: if undo/redo resurrected an ephemeral 'position' drawing, strip it cleanly.
        const all = this.chart.drawings.all();
        if (all.some((d) => isEphemeralOrderDrawing(d))) {
          const humanOnly = all.filter((d) => !isEphemeralOrderDrawing(d));
          this.suppressEvents = true;
          try {
            this.ephemeralIds.clear();
            this.chart.drawings.fromJSON({ version: 1, drawings: humanOnly });
          } finally {
            this.suppressEvents = false;
          }
        }
        void this.reconcileFullSnapshot();
      }),
      this.chart.on("drawing:selected", ({ id }) => {
        if (id) {
          if (this.ephemeralIds.has(id)) {
            this.onSelected?.(null);
            return;
          }
          const drawing = this.chart.drawings.all().find((d) => d.id === id);
          if (drawing && isEphemeralOrderDrawing(drawing)) {
            this.ephemeralIds.add(id);
            this.onSelected?.(null);
            return;
          }
        }
        const resolved = id ? (this.localToRemoteId.get(id) ?? id) : null;
        this.selectedRemoteId = resolved;
        this.onSelected?.(resolved);
      }),
    );
  }

  /** Select or clear a persisted drawing on the chart by its remote `drawing_id`. */
  selectDrawing(remoteDrawingId: string | null): void {
    if (remoteDrawingId === this.selectedRemoteId) return;
    if (remoteDrawingId === null) {
      this.selectedRemoteId = null;
      this.chart.drawings.select(null);
      return;
    }
    const all = this.chart.drawings.all();
    let localId = all.find((d) => d.id === remoteDrawingId)?.id ?? null;
    if (!localId) {
      for (const [k, v] of this.localToRemoteId.entries()) {
        if (v === remoteDrawingId) {
          localId = k;
          break;
        }
      }
    }
    if (!localId) return;
    this.selectedRemoteId = remoteDrawingId;
    this.chart.drawings.select(localId);
  }

  /**
   * Removes all ephemeral `'position'` order gestures AND resets Vela's undo history
   * via `fromJSON(humanOnly)` so `Ctrl+Z` can never resurrect a cleared order gesture
   * (Ticket 06 §1.3). All persisted HUMAN_DRAWING items stay intact.
   */
  clearEphemeralDrawings(): void {
    const all = this.chart.drawings.all();
    const hasEphemeral =
      this.ephemeralIds.size > 0 || all.some((d) => isEphemeralOrderDrawing(d));
    this.ephemeralIds.clear();
    if (!hasEphemeral) return;
    const humanOnly = all.filter((d) => !isEphemeralOrderDrawing(d));
    this.suppressEvents = true;
    try {
      this.chart.drawings.fromJSON({ version: 1, drawings: humanOnly });
    } finally {
      this.suppressEvents = false;
    }
  }

  exportHumanDrawingsDocument(): DrawingsDocument {
    return {
      version: 1,
      drawings: this.chart.drawings.all().filter((d) => !isEphemeralOrderDrawing(d)),
    };
  }

  setContext(instrumentId: string, timeframe: string, pricePrecision: number): void {
    this.instrumentId = instrumentId;
    this.timeframe = timeframe;
    this.pricePrecision = pricePrecision;
  }

  setReadOnly(readOnly: boolean): void {
    this.readOnly = readOnly;
    this.applyInteractiveState();
  }

  setReplayFilter(filter: ReplayDrawingFilter): void {
    this.replayFilter = { ...filter };
    this.applyInteractiveState();
    this.renderFromRemoteRecords();
  }

  private renderFromRemoteRecords(): void {
    this.suppressEvents = true;
    try {
      const visible = filterReplayDrawings(
        [...this.remoteRecords.values()],
        this.replayFilter,
      );
      const serialized = visible.map((r) => contractDrawingToSerialized(r));
      const doc: DrawingsDocument = { version: 1, drawings: serialized };
      this.chart.drawings.fromJSON(doc);
    } finally {
      this.suppressEvents = false;
    }
  }

  loadFromRecords(records: readonly DrawingRecord[]): void {
    this.suppressEvents = true;
    try {
      this.localToRemoteId.clear();
      this.remoteRecords.clear();
      for (const r of records) {
        if (r.deleted_at != null) continue;
        this.remoteRecords.set(r.drawing_id, r);
        this.localToRemoteId.set(r.drawing_id, r.drawing_id);
      }
      this.renderFromRemoteRecords();
      // Initial fetch and peer refreshes are remote loads; must not re-broadcast drawings-changed.
      this.onChanged?.([...this.remoteRecords.values()], "load");
    } finally {
      this.suppressEvents = false;
    }
  }

  async fetchAndLoad(): Promise<void> {
    try {
      const res = await this.client.drawings(this.instrumentId, this.timeframe, {
        all_timeframes: true,
      });
      this.loadFromRecords(res.drawings);
    } catch (error) {
      this.onError?.(
        error instanceof Error
          ? error.message
          : t("无法加载画线记录", "Failed to load persisted drawings"),
      );
    }
  }

  private scheduleCreate(localId: string): void {
    if (this.isEffectiveReadOnly()) return;
    this.clearTimer(localId);
    const timer = window.setTimeout(() => {
      this.debounceTimers.delete(localId);
      if (this.isEffectiveReadOnly()) return;
      const drawing = this.chart.drawings.all().find((d) => d.id === localId);
      if (!drawing) return;
      const payload = serializedDrawingToCreate(
        drawing,
        this.instrumentId,
        this.timeframe,
        this.pricePrecision,
      );
      void this.queue
        .run(async () => {
          const saved = await this.client.createDrawing(payload);
          this.localToRemoteId.set(localId, saved.drawing_id);
          this.remoteRecords.set(saved.drawing_id, saved);
          this.onChanged?.([...this.remoteRecords.values()], "mutation");
        })
        .catch((err) => {
          this.onError?.(err instanceof Error ? err.message : String(err));
        });
    }, 180);
    this.debounceTimers.set(localId, timer);
  }

  private scheduleEdit(localId: string): void {
    if (this.isEffectiveReadOnly()) return;
    this.clearTimer(localId);
    const timer = window.setTimeout(() => {
      this.debounceTimers.delete(localId);
      if (this.isEffectiveReadOnly()) return;
      const drawing = this.chart.drawings.all().find((d) => d.id === localId);
      if (!drawing) return;
      const remoteId = this.localToRemoteId.get(localId) ?? localId;
      const createPayload = serializedDrawingToCreate(
        drawing,
        this.instrumentId,
        this.timeframe,
        this.pricePrecision,
      );
      void this.queue
        .run(async () => {
          if (!this.remoteRecords.has(remoteId)) {
            const saved = await this.client.createDrawing(createPayload);
            this.localToRemoteId.set(localId, saved.drawing_id);
            this.remoteRecords.set(saved.drawing_id, saved);
          } else {
            const saved = await this.client.patchDrawing(remoteId, {
              visible_timeframes: createPayload.visible_timeframes,
              points: createPayload.points,
              text: createPayload.text,
              style: createPayload.style,
            });
            this.remoteRecords.set(saved.drawing_id, saved);
          }
          this.onChanged?.([...this.remoteRecords.values()], "mutation");
        })
        .catch((err) => {
          this.onError?.(err instanceof Error ? err.message : String(err));
        });
    }, 240);
    this.debounceTimers.set(localId, timer);
  }

  private scheduleDelete(localId: string): void {
    if (this.isEffectiveReadOnly()) return;
    this.clearTimer(localId);
    const remoteId = this.localToRemoteId.get(localId) ?? localId;
    if (!this.remoteRecords.has(remoteId)) return;
    void this.queue
      .run(async () => {
        await this.client.deleteDrawing(remoteId);
        this.localToRemoteId.delete(localId);
        this.remoteRecords.delete(remoteId);
        this.onChanged?.([...this.remoteRecords.values()], "mutation");
      })
      .catch((err) => {
        this.onError?.(err instanceof Error ? err.message : String(err));
      });
  }

  private async reconcileFullSnapshot(): Promise<void> {
    if (this.isEffectiveReadOnly()) return;
    const current = this.chart.drawings.all().filter((d) => !isEphemeralOrderDrawing(d));
    const presentRemoteIds = new Set(
      current.map((d) => this.localToRemoteId.get(d.id) ?? d.id),
    );
    await this.queue.run(async () => {
      for (const remoteId of [...this.remoteRecords.keys()]) {
        if (!presentRemoteIds.has(remoteId)) {
          await this.client.deleteDrawing(remoteId);
          this.remoteRecords.delete(remoteId);
        }
      }
      for (const drawing of current) {
        const remoteId = this.localToRemoteId.get(drawing.id) ?? drawing.id;
        const payload = serializedDrawingToCreate(
          drawing,
          this.instrumentId,
          this.timeframe,
          this.pricePrecision,
        );
        if (this.remoteRecords.has(remoteId)) {
          const saved = await this.client.patchDrawing(remoteId, {
            visible_timeframes: payload.visible_timeframes,
            points: payload.points,
            text: payload.text,
            style: payload.style,
          });
          this.remoteRecords.set(saved.drawing_id, saved);
        } else {
          const saved = await this.client.createDrawing(payload);
          this.localToRemoteId.set(drawing.id, saved.drawing_id);
          this.remoteRecords.set(saved.drawing_id, saved);
        }
      }
      this.onChanged?.([...this.remoteRecords.values()], "mutation");
    });
  }

  private clearTimer(localId: string): void {
    const timer = this.debounceTimers.get(localId);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      this.debounceTimers.delete(localId);
    }
  }

  async flush(): Promise<void> {
    await this.queue.idle();
  }

  destroy(): void {
    for (const timer of this.debounceTimers.values()) {
      window.clearTimeout(timer);
    }
    this.debounceTimers.clear();
    for (const unsub of this.unsubscribers) {
      unsub();
    }
    this.unsubscribers = [];
  }
}
