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
import type { DrawingSyncSource } from "./adapter";
import { createSerialQueue } from "./writeQueue";

const VELA_EXTENSION_PREFIX = "[talaria-vela:v1]";
const ALERT_ACTION_ID = "talaria.create_price_alert";
const ALERT_ICON_ID = "talaria-alert-bell";

let actionRegistered = false;
const activeAlertHandlers = new WeakMap<
  object,
  (payload: { drawingId: string; price: number | null; type: string }) => void
>();

export function ensureDrawingAlertActionRegistered(): void {
  if (actionRegistered) return;
  actionRegistered = true;

  registerIcon(
    ALERT_ICON_ID,
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
  );

  registerDrawingAction({
    id: ALERT_ACTION_ID,
    icon: ALERT_ICON_ID,
    label: t("创建价格预警", "Create price alert"),
    order: 0,
    surfaces: ["toolbar", "settings"],
    when: (drawing) => drawing.anchors.length > 0,
    run: (ctx, drawing) => {
      const handler = activeAlertHandlers.get(ctx.chart);
      const price = drawing.anchors[0]?.price ?? null;
      handler?.({ drawingId: drawing.id, price, type: drawing.type });
    },
  });
}

export function bindChartDrawingAlertHandler(
  chart: Vela,
  handler: (payload: { drawingId: string; price: number | null; type: string }) => void,
): () => void {
  ensureDrawingAlertActionRegistered();
  activeAlertHandlers.set(chart, handler);

  chart.renderer.setDrawingActions((drawingId: string) => {
    const drawing = chart.drawings.all().find((item) => item.id === drawingId);
    if (!drawing) return [];
    return drawingActions()
      .filter((desc) => !desc.when || desc.when(drawing))
      .map((desc) => {
        const surfaces = desc.surfaces ?? ["toolbar", "settings"];
        return {
          id: desc.id,
          icon: desc.icon,
          label: typeof desc.label === "string" ? desc.label : t("创建价格预警", "Create price alert"),
          toolbar: surfaces.includes("toolbar"),
          settings: surfaces.includes("settings"),
          run: () => {
            if (desc.id === ALERT_ACTION_ID) {
              const price = drawing.anchors[0]?.price ?? null;
              handler({ drawingId: drawing.id, price, type: drawing.type });
            }
          },
        };
      });
  });

  return () => {
    activeAlertHandlers.delete(chart);
    chart.renderer.setDrawingActions(null);
  };
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
  private debounceTimers = new Map<string, number>();
  private suppressEvents = false;
  private readOnly = false;
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

  private attachEvents(): void {
    this.unsubscribers.push(
      this.chart.on("drawing:created", ({ id }) => {
        if (this.suppressEvents || this.readOnly) return;
        this.scheduleCreate(id);
      }),
      this.chart.on("drawing:edited", ({ id }) => {
        if (this.suppressEvents || this.readOnly) return;
        this.scheduleEdit(id);
      }),
      this.chart.on("drawing:removed", ({ id }) => {
        if (this.suppressEvents || this.readOnly) return;
        this.scheduleDelete(id);
      }),
      this.chart.on("drawing:history", () => {
        if (this.suppressEvents || this.readOnly) return;
        void this.reconcileFullSnapshot();
      }),
      this.chart.on("drawing:selected", ({ id }) => {
        const resolved = id ? (this.localToRemoteId.get(id) ?? id) : null;
        this.onSelected?.(resolved);
      }),
    );
  }

  setContext(instrumentId: string, timeframe: string, pricePrecision: number): void {
    this.instrumentId = instrumentId;
    this.timeframe = timeframe;
    this.pricePrecision = pricePrecision;
  }

  setReadOnly(readOnly: boolean): void {
    this.readOnly = readOnly;
    this.chart.renderer.set("drawingsInteractive", !readOnly);
    this.chart.drawings.showToolbar(!readOnly);
  }

  loadFromRecords(records: readonly DrawingRecord[]): void {
    this.suppressEvents = true;
    try {
      this.localToRemoteId.clear();
      this.remoteRecords.clear();
      const serialized = records
        .filter((r) => r.deleted_at == null)
        .map((r) => {
          this.remoteRecords.set(r.drawing_id, r);
          this.localToRemoteId.set(r.drawing_id, r.drawing_id);
          return contractDrawingToSerialized(r);
        });
      const doc: DrawingsDocument = { version: 1, drawings: serialized };
      this.chart.drawings.fromJSON(doc);
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
    this.clearTimer(localId);
    const timer = window.setTimeout(() => {
      this.debounceTimers.delete(localId);
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
    this.clearTimer(localId);
    const timer = window.setTimeout(() => {
      this.debounceTimers.delete(localId);
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
    const current = this.chart.drawings.all();
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
