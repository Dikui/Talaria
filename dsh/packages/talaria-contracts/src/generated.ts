// 本文件由 `uv run talaria contract-check --write` 生成，请勿手改。

export type DecimalString = string;

export type PrecisionTier = "SECOND_VERIFIED" | "SESSION_BOUND" | "DATE_CONSERVATIVE";

export type BarStatus = "NORMAL" | "NO_TRADES" | "FEED_GAP";

export type FeedHealthStatus = "LIVE" | "STALE" | "RECOVERING";

export type SnapshotMode = "HISTORICAL_PIT" | "OBSERVED_REPLAY";

export type IndicatorName = "EMA" | "SMA" | "RSI" | "ATR" | "BOLL" | "VWAP" | "FUNDING_QUANTILE" | "OI_DIVERGENCE" | "NET_ODDS";

export type DerivativeKind = "mark_price" | "funding_rate" | "open_interest";

export type DrawingKind = "HORIZONTAL_LINE" | "RAY" | "SEGMENT" | "PARALLEL_CHANNEL" | "FIB_RETRACEMENT" | "RECTANGLE" | "TEXT_LABEL";

export type DrawingLineStyle = "solid" | "dashed" | "dotted";

export type KeyLevelKind = "SWING_HIGH" | "SWING_LOW" | "PREV_DAY_HIGH" | "PREV_DAY_LOW" | "PREV_DAY_CLOSE" | "WEEK_OPEN" | "MONTH_OPEN" | "OPENING_RANGE" | "SESSION_HIGH" | "SESSION_LOW" | "GAP" | "ROUND_NUMBER" | "POC" | "VAH" | "VAL" | "ANCHORED_VWAP" | "LIQUIDATION_ZONE";

export type KeyLevelCategory = "STRUCTURE" | "SESSION" | "GAP" | "ROUND_NUMBER" | "VOLUME" | "LIQUIDATION";

export type KeyLevelStatus = "ACTIVE" | "BROKEN" | "FLIPPED";

export type EvidenceClass = "OBSERVED" | "ESTIMATED";

export type WatchlistGroupKind = "MANUAL" | "SCANNER_FOCUS";

export type QuoteSource = "SUPERVISOR" | "SNAPSHOT" | "UNAVAILABLE";

export type DegradationReason = "QUOTA" | "NO_FEED";

export type AlertConditionKind = "METRIC_THRESHOLD" | "INDICATOR_SIGNAL" | "OVERLAY_CROSS" | "LEVEL_EVENT";

export type AlertLevelEvent = "TOUCH" | "BREAK" | "RETEST";

export type AlertMetric = "last_price" | "bid_price" | "ask_price" | "mark_price" | "funding_rate" | "open_interest";

export type AlertDirection = "ABOVE" | "BELOW";

export type AlertOverlayMode = "TOUCH" | "CLOSE_BREAK";

export type AlertStatus = "DRAFT" | "ARMED" | "COOLDOWN" | "EXPIRED" | "DISABLED";

export type ScannerConditionKind = "LIQUIDITY_ANOMALY" | "VOLATILITY_EXPANSION" | "FUNDING_EXTREME" | "OI_DIVERGENCE" | "KEY_LEVEL_EVENT";

export type ScannerSeverity = "INFO" | "WARNING" | "CRITICAL";

export type ScannerTriggerRoute = "LOCAL" | "CO_PILOT_CONSULT";

export type BriefingStatus = "PENDING" | "READY" | "ABSTAIN" | "FAILED";

export type AbstainReason = "STALE_SNAPSHOT" | "EVIDENCE_CONFLICT" | "GEOMETRY_INVERTED" | "MODEL_TIMEOUT" | "HOST_OFFLINE" | "MODEL_INVALID_OUTPUT";

export type ConsultationStatus = "PENDING" | "READY" | "ABSTAIN" | "FAILED";

export type SetupDirection = "LONG" | "SHORT" | "NEUTRAL";

export type EvidenceKind = "SNAPSHOT" | "CHART_VIEW" | "KEY_LEVEL" | "INDICATOR" | "OPERATOR" | "HUMAN_DRAWING";

export type AnnotationKind = "LEVEL" | "ZONE";

export type AnnotationStatus = "ACTIVE" | "ADOPTED" | "DISMISSED" | "ARCHIVED";

export interface TemporalStamp {
  ts_event: number;
  ts_published?: number | null;
  ts_available: number;
  precision: PrecisionTier;
  ts_ingested: number;
  ts_committed: number;
}
export interface InstrumentSpec {
  instrument_id: string;
  venue: string;
  symbol: string;
  asset_class: string;
  tick_size: DecimalString;
  qty_step: DecimalString;
  min_qty: DecimalString;
  price_precision: number;
  qty_precision: number;
}
export interface CompactBar {
  timestamp: number;
  open: DecimalString | null;
  high: DecimalString | null;
  low: DecimalString | null;
  close: DecimalString | null;
  volume: DecimalString | null;
  turnover: DecimalString | null;
  status: BarStatus;
  ts_available: number;
  precision: PrecisionTier;
}
export interface BarsQuery {
  instrument: string;
  timeframe: string;
  start: number;
  end?: number | null;
  as_of?: number | null;
  include_open?: boolean;
  tail_only?: boolean;
  resync_from?: number | null;
  available_since?: number | null;
}
export interface RangeBar {
  ts_event: number;
  open: DecimalString;
  high: DecimalString;
  low: DecimalString;
  close: DecimalString;
  volume: DecimalString;
  turnover: DecimalString;
  ts_available: number;
  precision: PrecisionTier;
  confirmed: boolean;
  revision: number;
}
export interface BarsResponse {
  instrument_id: string;
  timeframe: string;
  generation: number;
  bars: RangeBar[];
}
export interface KlineEvent {
  instrument_id: string;
  timeframe: string;
  bar: CompactBar;
  confirmed: boolean;
  revision: number;
}
export interface OrderBookEvent {
  instrument_id: string;
  bid_price: DecimalString;
  bid_size: DecimalString;
  ask_price: DecimalString;
  ask_size: DecimalString;
  ts_event: number;
  ts_ingested: number;
}
export interface DerivativeTickEvent {
  instrument_id: string;
  mark_price?: DecimalString | null;
  funding_rate?: DecimalString | null;
  next_funding_ts?: number | null;
  open_interest?: DecimalString | null;
  ts_event: number;
  ts_ingested: number;
}
export interface DerivativePoint {
  instrument_id: string;
  kind: DerivativeKind;
  timeframe?: string | null;
  value: DecimalString;
  ts_event: number;
  ts_published?: number | null;
  ts_available: number;
  precision?: PrecisionTier;
  ts_ingested: number;
  ts_committed: number;
}
export interface DerivativeSeries {
  kind: DerivativeKind;
  timeframe: string | null;
  points: DerivativePoint[];
}
export interface DerivativeSummary {
  mark_price?: DecimalString | null;
  mark_price_ts?: number | null;
  funding_rate?: DecimalString | null;
  funding_settled_ts?: number | null;
  next_funding_ts?: number | null;
  open_interest?: DecimalString | null;
  open_interest_ts?: number | null;
  oi_history_available?: boolean;
}
export interface DerivativeBundle {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  cutoff_ts: number;
  series: DerivativeSeries[];
  summary: DerivativeSummary;
}
export interface FeedHealthEvent {
  instrument_id: string;
  timeframe: string;
  status: FeedHealthStatus;
  ts: number;
  last_event_ts?: number | null;
  stale_since?: number | null;
  reconnect_count?: number;
}
export interface FeedHealthReport {
  feed_mode: string;
  status: FeedHealthStatus;
  channels: FeedHealthEvent[];
}
export interface SnapshotRequest {
  instrument_id: string;
  timeframes: string[];
  cutoff_ts?: number | null;
  mode?: SnapshotMode;
}
export interface SnapshotMeta {
  snapshot_id: string;
  instrument_id: string;
  timeframes: string[];
  cutoff_ts: number;
  created_ts: number;
  mode: SnapshotMode;
  bar_counts: Record<string, number>;
}
export interface ChartPage {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  bars: CompactBar[];
  has_more_forward: boolean;
  has_more_backward: boolean;
}
export interface ErrorResponse {
  code: string;
  message: string;
}
export interface IndicatorParams {
  period: number;
  std_dev?: DecimalString | null;
  anchor?: "snapshot" | "session" | null;
}
export interface IndicatorSummary {
  latest: Record<string, DecimalString | null>;
  warmup_bars: number;
  valid_points: number;
}
export interface IndicatorPoint {
  timestamp: number;
  values: Record<string, DecimalString | null>;
}
export interface IndicatorSeries {
  name: IndicatorName;
  pane: "main" | "sub";
  params: IndicatorParams;
  points: IndicatorPoint[];
  summary: IndicatorSummary;
}
export interface IndicatorRequest {
  snapshot_id: string;
  timeframe: string;
  names: string[];
  params?: Record<string, IndicatorParams> | null;
  limit?: number | null;
}
export interface OperatorRequest {
  snapshot_id: string;
  timeframe: string;
  operator: IndicatorName;
  params?: IndicatorParams | null;
  limit?: number | null;
}
export interface IndicatorBundle {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  series: IndicatorSeries[];
}
export interface IndicatorDefaultsResponse {
  defaults: Record<IndicatorName, IndicatorParams>;
}
export interface DrawingPoint {
  ts: number;
  price: DecimalString;
}
export interface DrawingStyle {
  color?: string;
  line_width?: number;
  line_style?: DrawingLineStyle;
  fill_opacity?: number;
}
export interface DrawingCreate {
  instrument_id: string;
  kind: DrawingKind;
  visible_timeframes?: string[];
  points: DrawingPoint[];
  text?: string;
  style?: DrawingStyle;
}
export interface DrawingPatch {
  visible_timeframes?: string[] | null;
  points?: DrawingPoint[] | null;
  text?: string | null;
  style?: DrawingStyle | null;
}
export interface DrawingRecord {
  drawing_id: string;
  instrument_id: string;
  kind: DrawingKind;
  visible_timeframes: string[];
  points: DrawingPoint[];
  text: string;
  style: DrawingStyle;
  version: number;
  created_ts: number;
  updated_ts: number;
  deleted_at?: number | null;
}
export interface DrawingListResponse {
  instrument_id: string;
  timeframe: string;
  drawings: DrawingRecord[];
}
export interface DrawingVersionList {
  drawing_id: string;
  versions: DrawingRecord[];
}
export interface DrawingClearResponse {
  instrument_id: string;
  cleared: number;
}
export interface ChartViewWindow {
  from_ts: number;
  to_ts: number;
  price_min: DecimalString;
  price_max: DecimalString;
}
export interface ChartView {
  instrument: InstrumentSpec;
  timeframe: string;
  window: ChartViewWindow;
  drawings: DrawingRecord[];
}
export interface KeyLevel {
  level_id: string;
  instrument_id: string;
  kind: KeyLevelKind;
  category: KeyLevelCategory;
  evidence_class?: EvidenceClass;
  timeframe: string;
  price?: DecimalString | null;
  price_low?: DecimalString | null;
  price_high?: DecimalString | null;
  strength: number;
  status: KeyLevelStatus;
  ts_available: number;
  formed_ts: number;
  method_version?: string;
  parameters?: Record<string, string>;
  touch_count: number;
  last_touch_ts?: number | null;
  volume_weight: DecimalString;
}
export interface KeyLevelSet {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  cutoff_ts: number;
  as_of_ts: number;
  method_version?: string;
  parameters?: Record<string, string>;
  notes?: string[];
  levels?: KeyLevel[];
}
export interface KeyLevelIgnoreRequest {
  instrument_id: string;
  level_id: string;
  ignored?: boolean;
}
export interface KeyLevelIgnoreRecord {
  instrument_id: string;
  level_id: string;
  ignored: boolean;
  updated_ts: number;
}
export interface KeyLevelIgnoreList {
  instrument_id: string;
  entries?: KeyLevelIgnoreRecord[];
}
export interface KeyLevelAdoptionRequest {
  snapshot_id: string;
  timeframe: string;
}
export interface KeyLevelAdoptionRecord {
  instrument_id: string;
  level_id: string;
  snapshot_id: string;
  drawing_id: string;
  adopted_ts: number;
  timeframe: string;
}
export interface KeyLevelFeedbackSummary {
  instrument_id: string;
  adopted_level_ids?: string[];
  ignored_level_ids?: string[];
}
export interface WatchlistGroupCreate {
  name: string;
}
export interface WatchlistGroupUpdate {
  name: string;
}
export interface WatchlistItemCreate {
  group_id: string;
  instrument_id: string;
}
export interface WatchlistOrder {
  ids: string[];
}
export interface WatchlistItemOrder {
  group_id: string;
  ids: string[];
}
export interface WatchlistItem {
  instrument_id: string;
  position: number;
  latest_price?: DecimalString | null;
  change_pct?: DecimalString | null;
  quote_ts?: number | null;
  quote_source: QuoteSource;
  realtime: boolean;
  feed_status?: FeedHealthStatus | null;
  stale: boolean;
  degraded: boolean;
  degradation_reason?: DegradationReason | null;
}
export interface WatchlistGroup {
  group_id: string;
  name: string;
  position: number;
  kind: WatchlistGroupKind;
  is_system: boolean;
  items: WatchlistItem[];
}
export interface Watchlist {
  groups: WatchlistGroup[];
  realtime_quota: number;
  realtime_used: number;
  generated_ts: number;
}
export interface AlertCondition {
  kind: AlertConditionKind;
  direction?: AlertDirection | null;
  metric?: AlertMetric | null;
  threshold?: DecimalString | null;
  indicator?: IndicatorName | null;
  indicator_period?: number | null;
  indicator_anchor?: "snapshot" | "session" | null;
  indicator_seed?: "pine" | "legacy_sma" | null;
  indicator_std_dev?: DecimalString | null;
  indicator_channel?: string | null;
  compare_indicator?: IndicatorName | null;
  compare_period?: number | null;
  compare_anchor?: "snapshot" | "session" | null;
  compare_seed?: "pine" | "legacy_sma" | null;
  compare_std_dev?: DecimalString | null;
  drawing_id?: string | null;
  overlay_mode?: AlertOverlayMode | null;
  overlay_ratio?: DecimalString | null;
  level_id?: string | null;
  level_event?: AlertLevelEvent | null;
  level_kind?: KeyLevelKind | null;
}
export interface AlertCreate {
  instrument_id: string;
  timeframe: string;
  condition: AlertCondition;
  note?: string;
  draft?: boolean;
  repeat?: boolean;
  cooldown_ms?: number;
  expires_at?: number | null;
}
export interface AlertPatch {
  condition?: AlertCondition | null;
  note?: string | null;
  repeat?: boolean | null;
  cooldown_ms?: number | null;
  expires_at?: number | null;
  enabled?: boolean | null;
}
export interface AlertRecord {
  alert_id: string;
  instrument_id: string;
  timeframe: string;
  condition: AlertCondition;
  status: AlertStatus;
  repeat: boolean;
  cooldown_ms: number;
  expires_at?: number | null;
  note?: string;
  created_ts: number;
  updated_ts: number;
  last_triggered_ts?: number | null;
  next_arm_ts?: number | null;
  trigger_count?: number;
}
export interface AlertListResponse {
  alerts: AlertRecord[];
}
export interface AlertTriggerRecord {
  trigger_id: string;
  alert_id: string;
  instrument_id: string;
  timeframe: string;
  condition_kind: AlertConditionKind;
  triggered_ts: number;
  snapshot_id: string;
  observed?: DecimalString | null;
  threshold?: DecimalString | null;
  message: string;
  note?: string;
}
export interface AlertTriggerListResponse {
  triggers: AlertTriggerRecord[];
}
export interface ScannerEvidence {
  metric: string;
  value: DecimalString;
  threshold?: DecimalString | null;
  note?: string | null;
}
export interface ScannerTrigger {
  trigger_id: string;
  instrument_id: string;
  timeframe: string;
  kind: ScannerConditionKind;
  severity: ScannerSeverity;
  title: string;
  summary: string;
  evidence?: ScannerEvidence[];
  tags?: string[];
  created_ts: number;
  snapshot_id: string;
  route?: ScannerTriggerRoute;
}
export interface ScannerTriggerListResponse {
  triggers: ScannerTrigger[];
  total: number;
  generated_ts: number;
}
export interface ScannerFocusItem {
  instrument_id: string;
  last_trigger_ts: number;
  trigger_count: number;
  severity: ScannerSeverity;
  kinds?: ScannerConditionKind[];
  last_title?: string | null;
  last_timeframe?: string | null;
  last_snapshot_id?: string | null;
}
export interface ScannerFocusResponse {
  group_id: string;
  name: string;
  items: ScannerFocusItem[];
  instrument_ids: string[];
  generated_ts: number;
}
export interface ScannerScanRequest {
  instrument_ids?: string[] | null;
}
export interface ScannerScanResponse {
  triggers: ScannerTrigger[];
  scanned_instruments: string[];
  generated_ts: number;
}
export interface BriefingLevelRef {
  label: string;
  price?: DecimalString | null;
  note?: string;
}
export interface BriefingContent {
  trigger_reason: string;
  key_levels?: BriefingLevelRef[];
  risks?: string[];
}
export interface ScannerBriefing {
  id: string;
  trigger_id: string;
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  status: BriefingStatus;
  summary?: BriefingContent | null;
  evidence_refs?: EvidenceRef[];
  abstain_reason?: string | null;
  created_ts: number;
  read_ts?: number | null;
}
export interface BriefingListResponse {
  briefings?: ScannerBriefing[];
  unread?: number;
  generated_ts: number;
}
export interface BriefingConsultRequest {
  question?: string | null;
}
export interface BriefingConsultResponse {
  briefing_id: string;
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  question: string;
}
export interface BriefingResultRequest {
  text?: string | null;
  content?: BriefingContent | null;
  abstain?: Abstain | null;
  error?: string | null;
  dsh_session_id?: string | null;
  dsh_turn_id?: string | null;
  usage?: UsageReport | null;
}
export interface CollabWindow {
  from_ts: number;
  to_ts: number;
  price_min: DecimalString;
  price_max: DecimalString;
}
export interface DrawingRef {
  drawing_id: string;
  kind: DrawingKind;
  points: DrawingPoint[];
  text?: string;
}
export interface CatalystEvent {
  event_id: string;
  title: string;
  ts: number;
  kind?: string;
  source?: string;
}
export interface EvidenceRef {
  evidence_id: string;
  kind: EvidenceKind;
  label: string;
  detail?: string;
  price_low?: DecimalString | null;
  price_high?: DecimalString | null;
}
export interface ConsultRequest {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  question: string;
  visible: CollabWindow;
  selection?: CollabWindow | null;
  drawing_ids?: string[];
}
export interface ConsultationPackage {
  consultation_id: string;
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  visible: CollabWindow;
  selection?: CollabWindow | null;
  drawings?: DrawingRef[];
  catalysts?: CatalystEvent[];
  question: string;
  snapshot_cutoff_ts: number;
  evidence_catalog?: EvidenceRef[];
  created_ts: number;
}
export interface SetupInsight {
  direction: SetupDirection;
  summary: string;
  entry_low: DecimalString;
  entry_high: DecimalString;
  invalidation_stop: DecimalString;
  take_profit: DecimalString[];
  max_position?: DecimalString | null;
  valid_until?: number | null;
  evidence_refs?: EvidenceRef[];
}
export interface Abstain {
  reason_code: AbstainReason;
  message: string;
  evidence_refs?: EvidenceRef[];
}
export interface ConsultationRecord {
  consultation_id: string;
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  status: ConsultationStatus;
  package: ConsultationPackage;
  insight?: SetupInsight | null;
  abstain?: Abstain | null;
  error?: string | null;
  dsh_session_id?: string | null;
  dsh_turn_id?: string | null;
  created_ts: number;
  updated_ts: number;
  completed_ts?: number | null;
}
export interface ConsultResponse {
  consultation_id: string;
  snapshot_id: string;
  status: ConsultationStatus;
}
export interface ConsultationListResponse {
  consultations?: ConsultationRecord[];
}
export interface HostResultRequest {
  text?: string | null;
  insight?: SetupInsight | null;
  abstain?: Abstain | null;
  error?: string | null;
  dsh_session_id?: string | null;
  dsh_turn_id?: string | null;
}
export interface UsageReport {
  dsh_session_id: string;
  dsh_turn_id: string;
  input_tokens?: number | null;
  cached_input_tokens?: number | null;
  output_tokens?: number | null;
  source?: string;
}
export interface UsageRow {
  dsh_session_id: string;
  dsh_turn_id: string;
  input_tokens?: number | null;
  cached_input_tokens?: number | null;
  output_tokens?: number | null;
  source: string;
  reported_ts: number;
}
export interface UsageReportResponse {
  recorded: boolean;
  dsh_session_id: string;
  dsh_turn_id: string;
  duplicate?: boolean;
}
export interface UsageSummary {
  dsh_session_id?: string | null;
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  turns?: number;
  sessions?: number;
  missing_reports?: number;
  updated_ts?: number;
}
export interface UsageSummaryResponse {
  summary: UsageSummary;
  sessions?: UsageSummary[];
  recent?: UsageRow[];
}
export interface AnnotationEvidence {
  level_id?: string | null;
  kline_ts?: number | null;
  note?: string;
}
export interface AgentAnnotationCreate {
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  session_id: string;
  round: number;
  kind: AnnotationKind;
  label: string;
  rationale?: string;
  price_low: DecimalString;
  price_high: DecimalString;
  ts_anchor: number;
  evidence_refs?: AnnotationEvidence[];
}
export interface AgentAnnotationRecord {
  annotation_id: string;
  snapshot_id: string;
  instrument_id: string;
  timeframe: string;
  session_id: string;
  round: number;
  kind: AnnotationKind;
  status: AnnotationStatus;
  label: string;
  rationale: string;
  price_low: DecimalString;
  price_high: DecimalString;
  ts_anchor: number;
  evidence_refs: AnnotationEvidence[];
  created_ts: number;
  updated_ts: number;
  adopted_drawing_id?: string | null;
  archived_reason?: string | null;
}
export interface AnnotationListResponse {
  annotations: AgentAnnotationRecord[];
}
export interface AnnotationSessionSummary {
  session_id: string;
  instrument_ids: string[];
  annotation_count: number;
  latest_ts: number;
}
export interface AnnotationSessionsResponse {
  sessions: AnnotationSessionSummary[];
}
export interface AnnotationAdoptionRecord {
  annotation_id: string;
  instrument_id: string;
  snapshot_id: string;
  timeframe: string;
  session_id: string;
  round: number;
  drawing_id: string;
  adopted_ts: number;
}
export interface AnnotationAdoptResponse {
  annotation: AgentAnnotationRecord;
  adoption: AnnotationAdoptionRecord;
}
export interface AnnotationClearRequest {
  instrument_id: string;
  session_id?: string | null;
}
export interface AnnotationClearResponse {
  instrument_id: string;
  session_id: string | null;
  cleared: number;
}
