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
  BarsQuery,
  BarsResponse,
  BriefingConsultResponse,
  BriefingListResponse,
  ChartPage,
  ChartView,
  ConsultationListResponse,
  ConsultationRecord,
  ConsultRequest,
  ConsultResponse,
  DerivativeBundle,
  DraftConfirmation,
  DraftContent,
  DraftDiscardRequest,
  DraftEdit,
  DraftRecord,
  DrawingClearResponse,
  DrawingCreate,
  DrawingListResponse,
  DrawingPatch,
  DrawingRecord,
  DrawingVersionList,
  ErrorResponse,
  FeedHealthReport,
  IndicatorBundle,
  IndicatorDefaultsResponse,
  IndicatorName,
  IndicatorParams,
  IndicatorSeries,
  InstrumentSpec,
  KeyLevelCategory,
  KeyLevelFeedbackSummary,
  KeyLevelIgnoreList,
  PriceScaleMode,
  StructureCategory,
  StructureFamily,
  StructureFeedbackSummary,
  StructureIgnoreList,
  StructureSet,
  KeyLevelSet,
  OperatorRequest,
  PaperAccountView,
  PaperCommand,
  PaperCommandRecord,
  PaperRound,
  RulePreviewRequest,
  RulePreviewResponse,
  RunEquityResponse,
  RunExportSummary,
  RunListResponse,
  RunRoundsResponse,
  ScannerBriefing,
  ScannerFocusResponse,
  ScannerScanResponse,
  ScannerTriggerListResponse,
  SizingPreviewRequest,
  SizingPreviewResponse,
  SnapshotMeta,
  TickKind,
  TicksJsonResponse,
  UsageSummaryResponse,
  Watchlist,
} from "@talaria/contracts";

import { t } from "./i18n";
import { serviceErrorMessage } from "./serviceErrorMessage";
import { getTauriServiceConnection, isTauriRuntime } from "./tauriBridge";

export interface HealthStatus {
  ok: boolean;
  version: string;
  feed_mode: string;
}

export interface CatalogInstrument {
  spec: InstrumentSpec;
  display_name?: string | null;
  base_asset?: string | null;
  quote_asset?: string | null;
  status?: string | null;
  registered: boolean;
}

export interface SearchInstrumentsResponse {
  pack: string;
  query: string;
  results: CatalogInstrument[];
}

export interface AddInstrumentResponse {
  created: boolean;
  instrument: InstrumentSpec;
  instruments: InstrumentSpec[];
}

export interface CollaborationStatus {
  mode: "solo" | "dual";
  host: "disabled" | "starting" | "online" | "offline" | "stopped" | string;
  url: string | null;
  detail?: string;
  message: string;
}

export interface DeskBootstrapInfo {
  token?: string | null;
  base_url?: string | null;
  feed_mode?: string | null;
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

export function getApiToken(): string | null {
  return apiToken ?? (readSessionToken() || null);
}

function apiPath(path: string): string {
  return apiBaseUrl ? `${apiBaseUrl}${path}` : path;
}

function authorizationHeader(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function readMetaContent(name: string): string {
  if (typeof document === "undefined") {
    return "";
  }
  const el = document.querySelector(`meta[name="${name}"]`);
  return el?.getAttribute("content")?.trim() ?? "";
}

function readWindowBootstrap(): { baseUrl: string; token: string } {
  if (typeof window === "undefined") {
    return { baseUrl: "", token: "" };
  }
  const bag = window.__TALARIA_BOOTSTRAP__;
  if (!bag) {
    return { baseUrl: "", token: "" };
  }
  return {
    baseUrl: (bag.baseUrl ?? bag.base_url ?? "").trim(),
    token: (bag.token ?? "").trim(),
  };
}

export function readSessionToken(): string {
  if (typeof document === "undefined") {
    return "";
  }
  const metaToken = readMetaContent("talaria-token");
  if (metaToken) {
    return metaToken;
  }
  const match = document.cookie.match(/(?:^|;\s*)talaria_token=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : "";
}

/**
 * Resolve loopback baseUrl & Bearer token for either Tauri v2 WebView or same-origin `/desk`.
 */
export async function initializeStandaloneApi(): Promise<void> {
  if (isTauriRuntime()) {
    const conn = await getTauriServiceConnection();
    if (conn?.token) {
      configureApi({ baseUrl: conn.base_url, token: conn.token });
      return;
    }
  }

  const injected = readWindowBootstrap();
  const metaBaseUrl = readMetaContent("talaria-base-url");
  if (injected.token) {
    configureApi({
      baseUrl: injected.baseUrl || metaBaseUrl,
      token: injected.token,
    });
    return;
  }

  let token = readSessionToken();
  if (token) {
    configureApi({ baseUrl: metaBaseUrl || apiBaseUrl, token });
    return;
  }

  // Try JSON bootstrap endpoint first (provided by talaria-server), then `/desk` cookie seed.
  try {
    const bootstrapRes = await fetch(apiPath("/api/desk/bootstrap"), {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (bootstrapRes.ok) {
      const data = (await bootstrapRes.json()) as DeskBootstrapInfo;
      if (data.token) {
        configureApi({
          baseUrl: data.base_url ?? (metaBaseUrl || apiBaseUrl),
          token: data.token,
        });
        return;
      }
    }
  } catch {
    // Fall back to /desk cookie bootstrap.
  }

  const deskRes = await fetch(apiPath("/desk"), { cache: "no-store" });
  if (deskRes.ok) {
    const html = await deskRes.text();
    const metaMatch = html.match(/<meta\s+name=["']talaria-token["']\s+content=["']([^"']+)["']/i);
    if (metaMatch?.[1]) {
      configureApi({ baseUrl: metaBaseUrl || apiBaseUrl, token: metaMatch[1] });
      return;
    }
    token = readSessionToken();
    if (token) {
      configureApi({ baseUrl: metaBaseUrl || apiBaseUrl, token });
      return;
    }
  }

  // Last check: open loopback service without auth (e.g. local Vite dev/mock proxy).
  try {
    const healthRes = await fetch(apiPath("/api/health"), { cache: "no-store" });
    if (healthRes.ok) {
      configureApi({ baseUrl: metaBaseUrl || apiBaseUrl, token: null });
      return;
    }
  } catch {
    // Ignore and raise explicit error below.
  }

  throw new Error(
    t(
      "无法获取 Talaria 会话凭据，请检查 talaria-data 服务是否正在运行后刷新重试。",
      "Unable to obtain Talaria session credentials. Ensure talaria-data is running and refresh.",
    ),
  );
}

async function parseError(response: Response): Promise<ApiError> {
  let body: Partial<ErrorResponse> | null = null;
  try {
    body = (await response.json()) as Partial<ErrorResponse>;
  } catch {
    body = null;
  }
  const code = body?.code ?? "HTTP_ERROR";
  return new ApiError(
    response.status,
    code,
    serviceErrorMessage(response.status, code, body?.message),
  );
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiPath(path), {
    signal,
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
  paperAccount: (account: string) =>
    getJson<PaperAccountView>(`/api/paper/accounts/${encodeURIComponent(account)}`),
  paperBarriers: (instrument: string) =>
    getJson<PaperRound[]>(`/api/paper/barriers?instrument=${encodeURIComponent(instrument)}`),
  paperDrafts: () => getJson<DraftRecord[]>("/api/paper/drafts"),
  savePaperDraft: (content: DraftContent) => postJson<DraftRecord>("/api/paper/drafts", content),
  editPaperDraft: (id: string, edit: DraftEdit) =>
    writeJson<DraftRecord>(`/api/paper/drafts/${encodeURIComponent(id)}`, "PUT", edit),
  confirmPaperDraft: (id: string, confirmation: DraftConfirmation) =>
    postJson<DraftRecord>(`/api/paper/drafts/${encodeURIComponent(id)}/confirm`, confirmation),
  discardPaperDraft: (id: string, discard: DraftDiscardRequest) =>
    postJson<DraftRecord>(`/api/paper/drafts/${encodeURIComponent(id)}/discard`, discard),
  paperCommands: (subAccountId?: string) =>
    getJson<PaperCommandRecord[]>(
      subAccountId
        ? `/api/paper/commands?sub_account_id=${encodeURIComponent(subAccountId)}`
        : "/api/paper/commands",
    ),
  submitPaperCommand: (command: PaperCommand) =>
    postJson<PaperCommandRecord>("/api/paper/commands", command),
  previewPaperSizing: (request: SizingPreviewRequest) =>
    postJson<SizingPreviewResponse>("/api/paper/sizing-preview", request),

  health(): Promise<HealthStatus> {
    return getJson<HealthStatus>("/api/health");
  },

  feedHealth(): Promise<FeedHealthReport> {
    return getJson<FeedHealthReport>("/api/feed/health");
  },

  collaboration(): Promise<CollaborationStatus> {
    return getJson<CollaborationStatus>("/api/collaboration");
  },

  instruments(): Promise<InstrumentSpec[]> {
    return getJson<InstrumentSpec[]>("/api/instruments");
  },

  searchInstruments(
    query: string,
    options?: { venue?: string; limit?: number },
  ): Promise<SearchInstrumentsResponse> {
    const params = new URLSearchParams();
    if (query.trim()) {
      params.set("q", query.trim());
    }
    if (options?.venue && options.venue.trim()) {
      params.set("venue", options.venue.trim());
    }
    if (options?.limit) {
      params.set("limit", String(options.limit));
    }
    const qs = params.toString();
    return getJson<SearchInstrumentsResponse>(
      qs ? `/api/instruments/search?${qs}` : "/api/instruments/search",
    );
  },

  addInstrument(spec: InstrumentSpec): Promise<AddInstrumentResponse> {
    return postJson<AddInstrumentResponse>("/api/instruments", { spec });
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
      ...(cutoffTs === undefined || cutoffTs === null ? {} : { cutoff_ts: cutoffTs }),
    });
  },

  rangeBars(query: BarsQuery): Promise<BarsResponse> {
    const params = new URLSearchParams({
      instrument: query.instrument,
      timeframe: query.timeframe,
      start: String(query.start),
    });
    if (query.end !== undefined && query.end !== null) {
      params.set("end", String(query.end));
    }
    if (query.as_of !== undefined && query.as_of !== null) {
      params.set("as_of", String(query.as_of));
    }
    if (query.include_open !== undefined) {
      params.set("include_open", String(query.include_open));
    }
    if (query.tail_only !== undefined) {
      params.set("tail_only", String(query.tail_only));
    }
    return getJson<BarsResponse>(`/api/bars?${params.toString()}`);
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
    if (live) query.set("live", "true");
    return getJson<IndicatorBundle>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/indicators?${query.toString()}`,
    );
  },

  indicatorsAt(options: {
    instrumentId: string;
    timeframe: string;
    names: string[];
    asOf?: number | null;
    limit?: number;
    live?: boolean;
  }): Promise<IndicatorBundle> {
    const query = new URLSearchParams({
      instrument_id: options.instrumentId,
      timeframe: options.timeframe,
      names: options.names.join(","),
      limit: String(options.limit ?? 300),
    });
    if (options.asOf !== undefined && options.asOf !== null) {
      query.set("as_of", String(options.asOf));
    }
    if (options.live) query.set("live", "true");
    return getJson<IndicatorBundle>(`/api/indicators?${query.toString()}`);
  },

  ticks(
    options: {
      instrument: string;
      kind?: TickKind;
      start: number;
      end: number;
      asOf?: number | null;
      limit?: number;
    },
    signal?: AbortSignal,
  ): Promise<TicksJsonResponse> {
    const query = new URLSearchParams({
      instrument: options.instrument,
      kind: options.kind ?? "trades",
      start: String(options.start),
      end: String(options.end),
      limit: String(options.limit ?? 2000),
    });
    if (options.asOf !== undefined && options.asOf !== null) {
      query.set("as_of", String(options.asOf));
    }
    return getJson<TicksJsonResponse>(`/api/ticks?${query.toString()}`, signal);
  },

  keyLevelsAt(options: {
    instrument: string;
    timeframe: string;
    asOf: number;
  }): Promise<KeyLevelSet> {
    const query = new URLSearchParams({
      instrument: options.instrument,
      timeframe: options.timeframe,
      as_of: String(options.asOf),
    });
    return getJson<KeyLevelSet>(`/api/key-levels/at?${query.toString()}`);
  },

  runs(): Promise<RunListResponse> {
    return getJson<RunListResponse>("/api/runs");
  },

  run(runId: string): Promise<RunExportSummary> {
    return getJson<RunExportSummary>(`/api/runs/${encodeURIComponent(runId)}`);
  },

  runRounds(runId: string): Promise<RunRoundsResponse> {
    return getJson<RunRoundsResponse>(`/api/runs/${encodeURIComponent(runId)}/rounds`);
  },

  runEquity(runId: string): Promise<RunEquityResponse> {
    return getJson<RunEquityResponse>(`/api/runs/${encodeURIComponent(runId)}/equity`);
  },

  computeOperator(request: OperatorRequest, live = false): Promise<IndicatorSeries> {
    const suffix = live ? "?live=true" : "";
    return postJson<IndicatorSeries>(`/api/operators/compute${suffix}`, request);
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

  derivatives(snapshotId: string, timeframe: string): Promise<DerivativeBundle> {
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

  previewRule(
    snapshotId: string,
    payload: RulePreviewRequest,
    signal: AbortSignal,
    client: string,
  ): Promise<RulePreviewResponse> {
    return writeJson<RulePreviewResponse>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/rule-preview`,
      "POST",
      payload,
      signal,
      { "X-Preview-Client": client },
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
    return deleteJson<DrawingRecord>(`/api/drawings/${encodeURIComponent(drawingId)}`);
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

  structures(
    snapshotId: string,
    timeframe: string,
    options: {
      families?: StructureFamily[];
      categories?: StructureCategory[];
      detectorIds?: string[];
      htf?: string[];
      scale?: PriceScaleMode;
      minStrength?: number;
      order?: string;
      limit?: number;
    } = {},
  ): Promise<StructureSet> {
    const params = new URLSearchParams({ timeframe });
    if (options.families !== undefined && options.families.length > 0) {
      params.set("families", options.families.join(","));
    }
    if (options.categories !== undefined && options.categories.length > 0) {
      params.set("categories", options.categories.join(","));
    }
    if (options.detectorIds !== undefined && options.detectorIds.length > 0) {
      params.set("detector_ids", options.detectorIds.join(","));
    }
    if (options.htf !== undefined && options.htf.length > 0) {
      params.set("htf", options.htf.join(","));
    }
    if (options.scale !== undefined) {
      params.set("scale", options.scale);
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
    return getJson<StructureSet>(
      `/api/snapshots/${encodeURIComponent(snapshotId)}/structures?${params.toString()}`,
    );
  },

  structuresAt(
    instrumentId: string,
    timeframe: string,
    asOf: number,
    options: {
      htf?: string[];
      scale?: PriceScaleMode;
      limit?: number;
    } = {},
  ): Promise<StructureSet> {
    const params = new URLSearchParams({
      instrument_id: instrumentId,
      timeframe,
      as_of: String(asOf),
    });
    if (options.htf !== undefined && options.htf.length > 0) {
      params.set("htf", options.htf.join(","));
    }
    if (options.scale !== undefined) {
      params.set("scale", options.scale);
    }
    if (options.limit !== undefined) {
      params.set("limit", String(options.limit));
    }
    return getJson<StructureSet>(`/api/structures/at?${params.toString()}`);
  },

  ignoredStructures(instrumentId: string): Promise<StructureIgnoreList> {
    return getJson<StructureIgnoreList>(
      `/api/structures/ignored?instrument_id=${encodeURIComponent(instrumentId)}`,
    );
  },

  setStructureIgnored(
    instrumentId: string,
    structureId: string,
    ignored: boolean,
  ): Promise<StructureIgnoreList> {
    return writeJson<StructureIgnoreList>("/api/structures/ignored", "PUT", {
      instrument_id: instrumentId,
      structure_id: structureId,
      ignored,
    });
  },

  structureFeedback(instrumentId: string): Promise<StructureFeedbackSummary> {
    return getJson<StructureFeedbackSummary>(
      `/api/structures/feedback?instrument_id=${encodeURIComponent(instrumentId)}`,
    );
  },

  adoptStructure(
    structureId: string,
    snapshotId: string,
    timeframe: string,
  ): Promise<DrawingRecord> {
    return writeJson<DrawingRecord>(
      `/api/structures/${encodeURIComponent(structureId)}/adopt`,
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
    const query = status === undefined ? "" : `?status=${encodeURIComponent(status)}`;
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
    return deleteJson<AlertRecord>(`/api/alerts/${encodeURIComponent(alertId)}`);
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
    return getJson<UsageSummaryResponse>(`/api/collaboration/usage?limit=${limit}`);
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
