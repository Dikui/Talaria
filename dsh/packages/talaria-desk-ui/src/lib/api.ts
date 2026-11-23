import type { DraftConfirmation, DraftContent, DraftEdit, DraftRecord, PaperAccountView, PaperRound } from "@talaria/contracts";
import type {
  AgentAnnotationRecord,
  AlertCreate,
  AlertListResponse,
  AlertPatch,
  AlertRecord,
  AlertStatus,
  AlertTriggerListResponse,
  AnnotationAdoptResponse,
  AnnotationClearResponse,
  AnnotationListResponse,
  AnnotationSessionsResponse,
  BriefingConsultResponse,
  BriefingListResponse,
  ChartPage,
  ChartView,
  ConsultationListResponse,
  ConsultationRecord,
  ConsultRequest,
  ConsultResponse,
  DerivativeBundle,
  DrawingClearResponse,
  DrawingCreate,
  DrawingListResponse,
  DrawingPatch,
  DrawingRecord,
  DrawingVersionList,
  ErrorResponse,
  IndicatorBundle,
  IndicatorDefaultsResponse,
  IndicatorName,
  IndicatorParams,
  IndicatorSeries,
  InstrumentSpec,
  KeyLevelCategory,
  KeyLevelFeedbackSummary,
  KeyLevelIgnoreList,
  KeyLevelSet,
  OperatorRequest,
  RulePreviewRequest,
  RulePreviewResponse,
  ScannerBriefing,
  ScannerFocusResponse,
  ScannerScanResponse,
  ScannerTriggerListResponse,
  SnapshotMeta,
  UsageSummaryResponse,
  Watchlist,
} from "@talaria/contracts";

import { t } from "./i18n";

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

/** Mount standalone API consumers only after the page has a writable session. */
export async function initializeStandaloneApi(): Promise<void> {
  let token = readSessionToken();
  if (!token) {
    const response = await fetch("/desk", { cache: "no-store" });
    token = response.ok ? readSessionToken() : "";
  }
  if (!token) throw new Error(t("无法获取 Talaria 会话凭据，请刷新页面后重试。", "Unable to obtain Talaria session credentials. Refresh the page and try again."));
  configureApi({});
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
    body?.message ?? t(`请求失败（HTTP ${response.status}）`, `Request failed (HTTP ${response.status})`),
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
  signal?: AbortSignal,
  headers?: Record<string, string>,
): Promise<T> {
  const response = await fetch(apiPath(path), {
    method,
    signal,
    headers: {
      Accept: "application/json",
      ...headers,
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
  paperAccounts: () => getJson<string[]>("/api/paper/accounts"),
  paperAccount: (account: string) => getJson<PaperAccountView>(`/api/paper/accounts/${encodeURIComponent(account)}`),
  paperBarriers: (instrument: string) =>
    getJson<PaperRound[]>(`/api/paper/barriers?instrument=${encodeURIComponent(instrument)}`),
  paperDrafts: () => getJson<DraftRecord[]>("/api/paper/drafts"),
  savePaperDraft: (content: DraftContent) => postJson<DraftRecord>("/api/paper/drafts", content),
  editPaperDraft: (id: string, edit: DraftEdit) =>
    writeJson<DraftRecord>(`/api/paper/drafts/${encodeURIComponent(id)}`, "PUT", edit),
  confirmPaperDraft: (id: string, confirmation: DraftConfirmation) =>
    postJson<DraftRecord>(`/api/paper/drafts/${encodeURIComponent(id)}/confirm`, confirmation),

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

  indicatorDefaults(): Promise<IndicatorDefaultsResponse> {
    return getJson<IndicatorDefaultsResponse>("/api/indicator-defaults");
  },

  setIndicatorDefault(
    name: IndicatorName,
    params: IndicatorParams,
  ): Promise<IndicatorDefaultsResponse> {
    return writeJson<IndicatorDefaultsResponse>(
      `/api/indicator-defaults/${encodeURIComponent(name)}`,
      "PUT",
      params,
    );
  },

  resetIndicatorDefault(name: IndicatorName): Promise<IndicatorDefaultsResponse> {
    return deleteJson<IndicatorDefaultsResponse>(
      `/api/indicator-defaults/${encodeURIComponent(name)}`,
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

  previewRule(snapshotId: string, payload: RulePreviewRequest, signal: AbortSignal, client: string): Promise<RulePreviewResponse> {
    return writeJson<RulePreviewResponse>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/rule-preview`, "POST", payload, signal, { "X-Preview-Client": client },
    );
  },

  drawings(
    instrumentId: string,
    timeframe: string,
    window: { from_ts?: number; to_ts?: number; all_timeframes?: boolean } = {},
  ): Promise<DrawingListResponse> {
    const query = new URLSearchParams({ instrument_id: instrumentId, timeframe });
    if (window.all_timeframes) query.set("all_timeframes", "true");
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

  keyLevelFeedback(instrumentId: string): Promise<KeyLevelFeedbackSummary> {
    return getJson<KeyLevelFeedbackSummary>(
      `/api/key-levels/feedback?instrument_id=${encodeURIComponent(instrumentId)}`,
    );
  },

  adoptKeyLevel(
    levelId: string,
    snapshotId: string,
    timeframe: string,
  ): Promise<DrawingRecord> {
    return writeJson<DrawingRecord>(
      `/api/key-levels/${encodeURIComponent(levelId)}/adopt`,
      "POST",
      { snapshot_id: snapshotId, timeframe },
    );
  },

  scannerTriggers(
    options: { limit?: number; instrumentId?: string } = {},
  ): Promise<ScannerTriggerListResponse> {
    const params = new URLSearchParams();
    if (options.limit !== undefined) {
      params.set("limit", String(options.limit));
    }
    if (options.instrumentId !== undefined) {
      params.set("instrument_id", options.instrumentId);
    }
    const suffix = params.toString();
    return getJson<ScannerTriggerListResponse>(
      `/api/scanner/triggers${suffix ? `?${suffix}` : ""}`,
    );
  },

  scannerFocus(): Promise<ScannerFocusResponse> {
    return getJson<ScannerFocusResponse>("/api/scanner/focus");
  },

  scanNow(): Promise<ScannerScanResponse> {
    return postJson<ScannerScanResponse>("/api/scanner/scan", {});
  },

  briefings(
    options: { limit?: number; unreadOnly?: boolean } = {},
  ): Promise<BriefingListResponse> {
    const params = new URLSearchParams();
    if (options.limit !== undefined) {
      params.set("limit", String(options.limit));
    }
    if (options.unreadOnly) {
      params.set("unread_only", "true");
    }
    const suffix = params.toString();
    return getJson<BriefingListResponse>(
      `/api/scanner/briefings${suffix ? `?${suffix}` : ""}`,
    );
  },

  markBriefingRead(briefingId: string): Promise<ScannerBriefing> {
    return postJson<ScannerBriefing>(
      `/api/scanner/briefings/${encodeURIComponent(briefingId)}/read`,
      {},
    );
  },

  briefingConsult(
    briefingId: string,
    question?: string,
  ): Promise<BriefingConsultResponse> {
    return postJson<BriefingConsultResponse>(
      `/api/scanner/briefings/${encodeURIComponent(briefingId)}/consult`,
      question ? { question } : {},
    );
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

  annotations(
    options: {
      instrumentId?: string;
      snapshotId?: string;
      sessionId?: string;
      includeArchived?: boolean;
    } = {},
  ): Promise<AnnotationListResponse> {
    const params = new URLSearchParams();
    if (options.instrumentId !== undefined) {
      params.set("instrument_id", options.instrumentId);
    }
    if (options.snapshotId !== undefined) {
      params.set("snapshot_id", options.snapshotId);
    }
    if (options.sessionId !== undefined) {
      params.set("session_id", options.sessionId);
    }
    if (options.includeArchived === true) {
      params.set("include_archived", "true");
    }
    const suffix = params.toString();
    return getJson<AnnotationListResponse>(
      `/api/annotations${suffix ? `?${suffix}` : ""}`,
    );
  },

  annotationSessions(): Promise<AnnotationSessionsResponse> {
    return getJson<AnnotationSessionsResponse>("/api/annotations/sessions");
  },

  adoptAnnotation(annotationId: string): Promise<AnnotationAdoptResponse> {
    return postJson<AnnotationAdoptResponse>(
      `/api/annotations/${encodeURIComponent(annotationId)}/adopt`,
      undefined,
    );
  },

  dismissAnnotation(annotationId: string): Promise<AgentAnnotationRecord> {
    return postJson<AgentAnnotationRecord>(
      `/api/annotations/${encodeURIComponent(annotationId)}/dismiss`,
      undefined,
    );
  },

  clearAnnotations(
    instrumentId: string,
    sessionId?: string,
  ): Promise<AnnotationClearResponse> {
    return postJson<AnnotationClearResponse>("/api/annotations/clear", {
      instrument_id: instrumentId,
      session_id: sessionId ?? null,
    });
  },

  consult(payload: ConsultRequest): Promise<ConsultResponse> {
    return postJson<ConsultResponse>("/api/collaboration/consult", payload);
  },

  consultation(consultationId: string): Promise<ConsultationRecord> {
    return getJson<ConsultationRecord>(
      `/api/collaboration/consultations/${encodeURIComponent(consultationId)}`,
    );
  },

  consultations(limit = 30): Promise<ConsultationListResponse> {
    return getJson<ConsultationListResponse>(
      `/api/collaboration/consultations?limit=${limit}`,
    );
  },

  collaborationUsage(limit = 200): Promise<UsageSummaryResponse> {
    return getJson<UsageSummaryResponse>(
      `/api/collaboration/usage?limit=${limit}`,
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
