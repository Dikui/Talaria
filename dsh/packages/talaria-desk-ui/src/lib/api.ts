import type {
  ChartPage,
  ErrorResponse,
  IndicatorBundle,
  IndicatorSeries,
  InstrumentSpec,
  OperatorRequest,
  SnapshotMeta,
  Watchlist,
} from "@talaria/contracts";

export interface HealthStatus {
  ok: boolean;
  version: string;
  feed_mode: string;
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
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw await parseError(response);
  }
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return writeJson<T>(path, "POST", body);
}

async function writeJson<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<T> {
  const token = readSessionToken();
  const response = await fetch(path, {
    method,
    headers: {
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    throw await parseError(response);
  }
  return (await response.json()) as T;
}

export const api = {
  health(): Promise<HealthStatus> {
    return getJson<HealthStatus>("/api/health");
  },

  instruments(): Promise<InstrumentSpec[]> {
    return getJson<InstrumentSpec[]>("/api/instruments");
  },

  createSnapshot(
    instrumentId: string,
    timeframes: string[],
  ): Promise<SnapshotMeta> {
    return postJson<SnapshotMeta>("/api/snapshots", {
      instrument_id: instrumentId,
      timeframes,
      mode: "HISTORICAL_PIT",
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
