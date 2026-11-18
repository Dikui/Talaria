import type {
  ChartPage,
  ErrorResponse,
  IndicatorBundle,
  IndicatorSeries,
  InstrumentSpec,
  OperatorRequest,
  SnapshotMeta,
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

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(apiPath(path), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...authorizationHeader(),
    },
    body: JSON.stringify(body),
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

  collaboration(): Promise<CollaborationStatus> {
    return getJson<CollaborationStatus>("/api/collaboration");
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
};
