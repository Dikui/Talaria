import type {
  AlertCreate,
  AlertListResponse,
  AlertPatch,
  AlertRecord,
  AlertStatus,
  AlertTriggerListResponse,
  ChartPage,
  ChartView,
  DerivativeBundle,
  DrawingClearResponse,
  DrawingCreate,
  DrawingListResponse,
  DrawingPatch,
  DrawingRecord,
  DrawingVersionList,
  ErrorResponse,
  IndicatorBundle,
  IndicatorSeries,
  InstrumentSpec,
  KeyLevelCategory,
  KeyLevelIgnoreList,
  KeyLevelSet,
  OperatorRequest,
  SnapshotMeta,
  Watchlist,
} from "@talaria/contracts";

export interface HealthStatus {
  ok: boolean;
  version: string;
  feed_mode: string;
}

/** ``/api/collaboration``：宿主状态只做展示，不参与图表快通道。 */
export interface CollaborationStatus {
  mode: "solo" | "dual";
  host: "disabled" | "starting" | "online" | "offline" | "stopped" | string;
  url: string | null;
  detail?: string;
  message: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

let apiBaseUrl = "";
let apiToken: string | null = null;

/** 嵌入 DSH 时由插件注入回环守护进程地址与短期 Bearer token。 */
export function configureApi(config: {
  baseUrl?: string;
  token?: string | null;
}): void {
  apiBaseUrl = (config.baseUrl ?? "").replace(/\/$/, "");
  apiToken = config.token ?? null;
}

export function getApiBaseUrl(): string {
  return apiBaseUrl;
}

function apiPath(path: string): string {
  return apiBaseUrl ? `${apiBaseUrl}${path}` : path;
}

function authorizationHeader(): Record<string, string> {
  const token = apiToken ?? readSessionToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function readSessionToken(): string {
  if (typeof document === "undefined") {
    return "";
  }
  const match = document.cookie.match(/(?:^|;\s*)talaria_token=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function parseError(response: Response): Promise<ApiError> {
  let body: Partial<ErrorResponse> | null = null;
  try {
    body = (await response.json()) as Partial<ErrorResponse>;
  } catch {
    body = null;
  }
  return new ApiError(
    response.status,
    body?.code ?? "HTTP_ERROR",
    body?.message ?? `请求失败（HTTP ${response.status}）`,
  );
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(apiPath(path), {
    headers: { Accept: "application/json", ...authorizationHeader() },
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  return (await response.json()) as T;
}

async function writeJson<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<T> {
  const response = await fetch(apiPath(path), {
    method,
    headers: {
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...authorizationHeader(),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return writeJson<T>(path, "POST", body);
}

async function patchJson<T>(path: string, body: unknown): Promise<T> {
  return writeJson<T>(path, "PATCH", body);
}

async function deleteJson<T>(path: string): Promise<T> {
  return writeJson<T>(path, "DELETE");
}

export const api = {
  health(): Promise<HealthStatus> {
    return getJson<HealthStatus>("/api/health");
  },

  collaboration(): Promise<CollaborationStatus> {
    return getJson<CollaborationStatus>("/api/collaboration");
  },

  instruments(): Promise<InstrumentSpec[]> {
    return getJson<InstrumentSpec[]>("/api/instruments");
  },

  createSnapshot(
    instrumentId: string,
    timeframes: string[],
    cutoffTs?: number | null,
  ): Promise<SnapshotMeta> {
    return postJson<SnapshotMeta>("/api/snapshots", {
      instrument_id: instrumentId,
      timeframes,
      mode: "HISTORICAL_PIT",
      ...(cutoffTs === undefined || cutoffTs === null
        ? {}
        : { cutoff_ts: cutoffTs }),
    });
  },

  indicators(
    snapshotId: string,
    timeframe: string,
    names: string[],
    limit = 300,
    live = false,
  ): Promise<IndicatorBundle> {
    const query = new URLSearchParams({
      timeframe,
      names: names.join(","),
      limit: String(limit),
    });
    if (live) query.set("live", "1");
    return getJson<IndicatorBundle>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/indicators?${query.toString()}`,
    );
  },

  computeOperator(
    request: OperatorRequest,
    live = false,
  ): Promise<IndicatorSeries> {
    const suffix = live ? "?live=1" : "";
    return postJson<IndicatorSeries>(
      `/api/operators/compute${suffix}`,
      request,
    );
  },

  derivatives(
    snapshotId: string,
    timeframe: string,
  ): Promise<DerivativeBundle> {
    const query = new URLSearchParams({ timeframe });
    return getJson<DerivativeBundle>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/derivatives?${query.toString()}`,
    );
  },

  bars(
    snapshotId: string,
    timeframe: string,
    options: { limit?: number; beforeTs?: number } = {},
  ): Promise<ChartPage> {
    const query = new URLSearchParams({
      timeframe,
      limit: String(options.limit ?? 300),
    });
    if (options.beforeTs !== undefined) {
      query.set("before_ts", String(options.beforeTs));
    }
    return getJson<ChartPage>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/bars?${query.toString()}`,
    );
  },

  drawings(
    instrumentId: string,
    timeframe: string,
    window: { from_ts?: number; to_ts?: number } = {},
  ): Promise<DrawingListResponse> {
    const query = new URLSearchParams({ instrument_id: instrumentId, timeframe });
    if (window.from_ts !== undefined) query.set("from_ts", String(window.from_ts));
    if (window.to_ts !== undefined) query.set("to_ts", String(window.to_ts));
    return getJson<DrawingListResponse>(`/api/drawings?${query.toString()}`);
  },

  chartView(params: {
    instrument_id: string;
    timeframe: string;
    from_ts: number;
    to_ts: number;
    price_min: string;
    price_max: string;
  }): Promise<ChartView> {
    const query = new URLSearchParams({
      instrument_id: params.instrument_id,
      timeframe: params.timeframe,
      from_ts: String(params.from_ts),
      to_ts: String(params.to_ts),
      price_min: params.price_min,
      price_max: params.price_max,
    });
    return getJson<ChartView>(`/api/chart-view?${query.toString()}`);
  },

  createDrawing(payload: DrawingCreate): Promise<DrawingRecord> {
    return postJson<DrawingRecord>("/api/drawings", payload);
  },

  patchDrawing(drawingId: string, patch: DrawingPatch): Promise<DrawingRecord> {
    return patchJson<DrawingRecord>(
      `/api/drawings/${encodeURIComponent(drawingId)}`,
      patch,
    );
  },

  deleteDrawing(drawingId: string): Promise<DrawingRecord> {
    return deleteJson<DrawingRecord>(
      `/api/drawings/${encodeURIComponent(drawingId)}`,
    );
  },

  restoreDrawing(drawingId: string): Promise<DrawingRecord> {
    return postJson<DrawingRecord>(
      `/api/drawings/${encodeURIComponent(drawingId)}/restore`,
      undefined,
    );
  },

  drawingVersions(drawingId: string): Promise<DrawingVersionList> {
    return getJson<DrawingVersionList>(
      `/api/drawings/${encodeURIComponent(drawingId)}/versions`,
    );
  },

  clearDrawings(instrumentId: string): Promise<DrawingClearResponse> {
    return deleteJson<DrawingClearResponse>(
      `/api/drawings?instrument_id=${encodeURIComponent(instrumentId)}`,
    );
  },

  keyLevels(
    snapshotId: string,
    timeframe: string,
    options: {
      categories?: KeyLevelCategory[];
      minStrength?: number;
      order?: string;
      limit?: number;
    } = {},
  ): Promise<KeyLevelSet> {
    const params = new URLSearchParams({ timeframe });
    if (options.categories !== undefined && options.categories.length > 0) {
      params.set("categories", options.categories.join(","));
    }
    if (options.minStrength !== undefined) {
      params.set("min_strength", String(options.minStrength));
    }
    if (options.order !== undefined) {
      params.set("order", options.order);
    }
    if (options.limit !== undefined) {
      params.set("limit", String(options.limit));
    }
    return getJson<KeyLevelSet>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/key-levels?${params.toString()}`,
    );
  },

  ignoredKeyLevels(instrumentId: string): Promise<KeyLevelIgnoreList> {
    return getJson<KeyLevelIgnoreList>(
      `/api/key-levels/ignored?instrument_id=${encodeURIComponent(instrumentId)}`,
    );
  },

  setKeyLevelIgnored(
    instrumentId: string,
    levelId: string,
    ignored: boolean,
  ): Promise<KeyLevelIgnoreList> {
    return writeJson<KeyLevelIgnoreList>("/api/key-levels/ignored", "PUT", {
      instrument_id: instrumentId,
      level_id: levelId,
      ignored,
    });
  },

  alerts(status?: AlertStatus): Promise<AlertListResponse> {
    const query =
      status === undefined ? "" : `?status=${encodeURIComponent(status)}`;
    return getJson<AlertListResponse>(`/api/alerts${query}`);
  },

  alertTriggers(
    options: { alertId?: string; instrumentId?: string; limit?: number } = {},
  ): Promise<AlertTriggerListResponse> {
    const params = new URLSearchParams();
    if (options.alertId !== undefined) {
      params.set("alert_id", options.alertId);
    }
    if (options.instrumentId !== undefined) {
      params.set("instrument_id", options.instrumentId);
    }
    if (options.limit !== undefined) {
      params.set("limit", String(options.limit));
    }
    const suffix = params.toString();
    return getJson<AlertTriggerListResponse>(
      `/api/alerts/triggers${suffix ? `?${suffix}` : ""}`,
    );
  },

  createAlert(payload: AlertCreate): Promise<AlertRecord> {
    return postJson<AlertRecord>("/api/alerts", payload);
  },

  patchAlert(alertId: string, patch: AlertPatch): Promise<AlertRecord> {
    return patchJson<AlertRecord>(
      `/api/alerts/${encodeURIComponent(alertId)}`,
      patch,
    );
  },

  deleteAlert(alertId: string): Promise<AlertRecord> {
    return deleteJson<AlertRecord>(
      `/api/alerts/${encodeURIComponent(alertId)}`,
    );
  },

  confirmAlert(alertId: string): Promise<AlertRecord> {
    return postJson<AlertRecord>(
      `/api/alerts/${encodeURIComponent(alertId)}/confirm`,
      undefined,
    );
  },

  dismissAlert(alertId: string): Promise<AlertRecord> {
    return postJson<AlertRecord>(
      `/api/alerts/${encodeURIComponent(alertId)}/dismiss`,
      undefined,
    );
  },

  watchlist(): Promise<Watchlist> {
    return getJson<Watchlist>("/api/watchlist");
  },

  createWatchlistGroup(name: string): Promise<Watchlist> {
    return writeJson<Watchlist>("/api/watchlist/groups", "POST", { name });
  },

  renameWatchlistGroup(groupId: string, name: string): Promise<Watchlist> {
    return writeJson<Watchlist>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      "PATCH",
      { name },
    );
  },

  deleteWatchlistGroup(groupId: string): Promise<Watchlist> {
    return writeJson<Watchlist>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      "DELETE",
    );
  },

  orderWatchlistGroups(ids: string[]): Promise<Watchlist> {
    return writeJson<Watchlist>("/api/watchlist/groups/order", "PUT", { ids });
  },

  addWatchlistItem(groupId: string, instrumentId: string): Promise<Watchlist> {
    return writeJson<Watchlist>("/api/watchlist/items", "POST", {
      group_id: groupId,
      instrument_id: instrumentId,
    });
  },

  removeWatchlistItem(instrumentId: string): Promise<Watchlist> {
    return writeJson<Watchlist>(
      `/api/watchlist/items/${encodeURIComponent(instrumentId)}`,
      "DELETE",
    );
  },

  orderWatchlistItems(groupId: string, ids: string[]): Promise<Watchlist> {
    return writeJson<Watchlist>("/api/watchlist/items/order", "PUT", {
      group_id: groupId,
      ids,
    });
  },
};
