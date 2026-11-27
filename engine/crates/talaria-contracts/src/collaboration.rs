//! Co-pilot consultation, briefing, agent annotation, usage, and paper draft/round contracts.

use serde::{Deserialize, Serialize};

use crate::bars::CompactBar;
use crate::decimal::{
    DecimalStr, serde_decimal_str, serde_decimal_str_opt, serde_decimal_str_vec,
    serde_decimal_str_vec_opt,
};
use crate::drawings::{DrawingKind, DrawingPoint};
use crate::market::Direction;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AbstainReason {
    StaleSnapshot,
    EvidenceConflict,
    GeometryInverted,
    ModelTimeout,
    HostOffline,
    ModelInvalidOutput,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ConsultationStatus {
    Pending,
    Ready,
    Abstain,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BriefingStatus {
    Pending,
    Ready,
    Abstain,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SetupDirection {
    Long,
    Short,
    Neutral,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EvidenceKind {
    Snapshot,
    ChartView,
    KeyLevel,
    Indicator,
    Operator,
    HumanDrawing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AnnotationKind {
    Level,
    Zone,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AnnotationStatus {
    Active,
    Adopted,
    Dismissed,
    Archived,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CollabWindow {
    pub from_ts: i64,
    pub to_ts: i64,
    #[serde(with = "serde_decimal_str")]
    pub price_min: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub price_max: DecimalStr,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingRef {
    pub drawing_id: String,
    pub kind: DrawingKind,
    pub points: Vec<DrawingPoint>,
    #[serde(default)]
    pub text: String,
}

fn default_catalyst_kind() -> String {
    "CATALYST".to_owned()
}

fn default_catalyst_source() -> String {
    "catalyst_calendar".to_owned()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalystEvent {
    pub event_id: String,
    pub title: String,
    pub ts: i64,
    #[serde(default = "default_catalyst_kind")]
    pub kind: String,
    #[serde(default = "default_catalyst_source")]
    pub source: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceRef {
    pub evidence_id: String,
    pub kind: EvidenceKind,
    pub label: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub price_low: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub price_high: Option<DecimalStr>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConsultRequest {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub question: String,
    pub visible: CollabWindow,
    #[serde(default)]
    pub selection: Option<CollabWindow>,
    #[serde(default)]
    pub drawing_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConsultationPackage {
    pub consultation_id: String,
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub visible: CollabWindow,
    #[serde(default)]
    pub selection: Option<CollabWindow>,
    #[serde(default)]
    pub drawings: Vec<DrawingRef>,
    #[serde(default)]
    pub catalysts: Vec<CatalystEvent>,
    pub question: String,
    pub snapshot_cutoff_ts: i64,
    #[serde(default)]
    pub evidence_catalog: Vec<EvidenceRef>,
    pub created_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetupInsight {
    pub direction: SetupDirection,
    pub summary: String,
    #[serde(with = "serde_decimal_str")]
    pub entry_low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub entry_high: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub invalidation_stop: DecimalStr,
    #[serde(with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub max_position: Option<DecimalStr>,
    #[serde(default)]
    pub valid_until: Option<i64>,
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Abstain {
    pub reason_code: AbstainReason,
    pub message: String,
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceRef>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConsultationRecord {
    pub consultation_id: String,
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub status: ConsultationStatus,
    pub package: ConsultationPackage,
    #[serde(default)]
    pub insight: Option<SetupInsight>,
    #[serde(default)]
    pub abstain: Option<Abstain>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub dsh_session_id: Option<String>,
    #[serde(default)]
    pub dsh_turn_id: Option<String>,
    pub created_ts: i64,
    pub updated_ts: i64,
    #[serde(default)]
    pub completed_ts: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConsultResponse {
    pub consultation_id: String,
    pub snapshot_id: String,
    pub status: ConsultationStatus,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConsultationListResponse {
    #[serde(default)]
    pub consultations: Vec<ConsultationRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostResultRequest {
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub insight: Option<SetupInsight>,
    #[serde(default)]
    pub abstain: Option<Abstain>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub dsh_session_id: Option<String>,
    #[serde(default)]
    pub dsh_turn_id: Option<String>,
}

fn default_usage_source() -> String {
    "host".to_owned()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageReport {
    pub dsh_session_id: String,
    pub dsh_turn_id: String,
    #[serde(default)]
    pub input_tokens: Option<u64>,
    #[serde(default)]
    pub cached_input_tokens: Option<u64>,
    #[serde(default)]
    pub output_tokens: Option<u64>,
    #[serde(default = "default_usage_source")]
    pub source: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageRow {
    pub dsh_session_id: String,
    pub dsh_turn_id: String,
    #[serde(default)]
    pub input_tokens: Option<u64>,
    #[serde(default)]
    pub cached_input_tokens: Option<u64>,
    #[serde(default)]
    pub output_tokens: Option<u64>,
    pub source: String,
    pub reported_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageReportResponse {
    pub recorded: bool,
    pub dsh_session_id: String,
    pub dsh_turn_id: String,
    #[serde(default)]
    pub duplicate: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageSummary {
    #[serde(default)]
    pub dsh_session_id: Option<String>,
    #[serde(default)]
    pub input_tokens: u64,
    #[serde(default)]
    pub cached_input_tokens: u64,
    #[serde(default)]
    pub output_tokens: u64,
    #[serde(default)]
    pub turns: u64,
    #[serde(default)]
    pub sessions: u64,
    #[serde(default)]
    pub missing_reports: u64,
    #[serde(default)]
    pub updated_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageSummaryResponse {
    pub summary: UsageSummary,
    #[serde(default)]
    pub sessions: Vec<UsageSummary>,
    #[serde(default)]
    pub recent: Vec<UsageRow>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BriefingLevelRef {
    pub label: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub price: Option<DecimalStr>,
    #[serde(default)]
    pub note: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BriefingContent {
    pub trigger_reason: String,
    #[serde(default)]
    pub key_levels: Vec<BriefingLevelRef>,
    #[serde(default)]
    pub risks: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerBriefing {
    pub id: String,
    pub trigger_id: String,
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub status: BriefingStatus,
    #[serde(default)]
    pub summary: Option<BriefingContent>,
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceRef>,
    #[serde(default)]
    pub abstain_reason: Option<String>,
    pub created_ts: i64,
    #[serde(default)]
    pub read_ts: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BriefingListResponse {
    #[serde(default)]
    pub briefings: Vec<ScannerBriefing>,
    #[serde(default)]
    pub unread: usize,
    pub generated_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BriefingConsultRequest {
    #[serde(default)]
    pub question: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BriefingConsultResponse {
    pub briefing_id: String,
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub question: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BriefingResultRequest {
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub content: Option<BriefingContent>,
    #[serde(default)]
    pub abstain: Option<Abstain>,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub dsh_session_id: Option<String>,
    #[serde(default)]
    pub dsh_turn_id: Option<String>,
    #[serde(default)]
    pub usage: Option<UsageReport>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationEvidence {
    #[serde(default)]
    pub structure_id: Option<String>,
    #[serde(default)]
    pub level_id: Option<String>,
    #[serde(default)]
    pub kline_ts: Option<i64>,
    #[serde(default)]
    pub note: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentAnnotationCreate {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub session_id: String,
    pub round: u64,
    pub kind: AnnotationKind,
    pub label: String,
    #[serde(default)]
    pub rationale: String,
    #[serde(with = "serde_decimal_str")]
    pub price_low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub price_high: DecimalStr,
    pub ts_anchor: i64,
    #[serde(default)]
    pub evidence_refs: Vec<AnnotationEvidence>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentAnnotationRecord {
    pub annotation_id: String,
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub session_id: String,
    pub round: u64,
    pub kind: AnnotationKind,
    pub status: AnnotationStatus,
    pub label: String,
    pub rationale: String,
    #[serde(with = "serde_decimal_str")]
    pub price_low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub price_high: DecimalStr,
    pub ts_anchor: i64,
    pub evidence_refs: Vec<AnnotationEvidence>,
    pub created_ts: i64,
    pub updated_ts: i64,
    #[serde(default)]
    pub adopted_drawing_id: Option<String>,
    #[serde(default)]
    pub archived_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationListResponse {
    pub annotations: Vec<AgentAnnotationRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationSessionSummary {
    pub session_id: String,
    pub instrument_ids: Vec<String>,
    pub annotation_count: usize,
    pub latest_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationSessionsResponse {
    pub sessions: Vec<AnnotationSessionSummary>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationAdoptionRecord {
    pub annotation_id: String,
    pub instrument_id: String,
    pub snapshot_id: String,
    pub timeframe: String,
    pub session_id: String,
    pub round: u64,
    pub drawing_id: String,
    pub adopted_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationAdoptResponse {
    pub annotation: AgentAnnotationRecord,
    pub adoption: AnnotationAdoptionRecord,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationClearRequest {
    pub instrument_id: String,
    #[serde(default)]
    pub session_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnnotationClearResponse {
    pub instrument_id: String,
    pub session_id: Option<String>,
    pub cleared: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TrailingStopSpec {
    #[serde(with = "serde_decimal_str")]
    pub activation_pct: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub distance_pct: DecimalStr,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExitBarrierSpec {
    #[serde(with = "serde_decimal_str")]
    pub stop: DecimalStr,
    #[serde(with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    pub max_holding_ms: i64,
    #[serde(default)]
    pub trailing_stop: Option<TrailingStopSpec>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftContent {
    pub sub_account_id: String,
    pub instrument_id: String,
    pub snapshot_id: String,
    #[serde(default)]
    pub consultation_id: Option<String>,
    pub direction: Direction,
    #[serde(with = "serde_decimal_str")]
    pub entry_low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub entry_high: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub invalidation_stop: DecimalStr,
    #[serde(with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    #[serde(default)]
    pub valid_until: Option<i64>,
    #[serde(default)]
    pub round_id: Option<String>,
    #[serde(default)]
    pub inherited_barriers: Option<ExitBarrierSpec>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftEdit {
    pub version: u64,
    pub content: DraftContent,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftConfirmation {
    pub version: u64,
    pub content_hash: String,
    pub confirmed_by: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfirmedDraft {
    pub version: u64,
    pub content_hash: String,
    pub confirmed_by: String,
    pub draft_id: String,
    pub content: DraftContent,
    pub basis_cutoff_ts: i64,
    pub created_ts: i64,
    pub confirmed_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftDiscardRequest {
    pub version: u64,
    #[serde(default)]
    pub discarded_by: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftReceipt {
    pub draft_id: String,
    pub version: u64,
    pub accepted: bool,
    #[serde(default)]
    pub plan_id: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub snapshot_id: Option<String>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        with = "serde_decimal_str_opt"
    )]
    pub quantity: Option<DecimalStr>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DraftStatus {
    Staged,
    Modified,
    Confirmed,
    Discarded,
    Expired,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TradingState {
    Active,
    Reducing,
    Halted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MarginMode {
    Isolated,
    Cross,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PaperProcessState {
    Running,
    Stopped,
    Offline,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PaperCommandKind {
    TightenStop,
    MoveToBreakeven,
    WidenStop,
    ModifyTakeProfit,
    CancelEntry,
    CloseAll,
    ReduceRatio,
    SetTradingState,
}

fn default_currency() -> String {
    "USDT".to_owned()
}

fn default_true() -> bool {
    true
}

fn default_process_state() -> PaperProcessState {
    PaperProcessState::Running
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftRecord {
    pub draft_id: String,
    pub version: u64,
    pub content_hash: String,
    pub content: DraftContent,
    pub status: DraftStatus,
    pub claimed: bool,
    #[serde(default)]
    pub receipt: Option<DraftReceipt>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftDeliveryRequest {
    pub instrument_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftEvidence {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub cutoff_ts: i64,
    pub bars: Vec<CompactBar>,
    #[serde(default)]
    pub current_bar: Option<CompactBar>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperRound {
    pub round_id: String,
    pub sub_account_id: String,
    pub instrument_id: String,
    pub direction: Direction,
    #[serde(with = "serde_decimal_str")]
    pub quantity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub stop: DecimalStr,
    #[serde(default, with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    #[serde(default)]
    pub phase: Option<String>,
    #[serde(default)]
    pub barriers: Option<ExitBarrierSpec>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub entry_low: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub entry_high: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub entry_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub mark_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub unrealized_pnl: Option<DecimalStr>,
    #[serde(default)]
    pub opened_ts: Option<i64>,
    #[serde(default)]
    pub closed_ts: Option<i64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PaperFillKind {
    EntryFilled,
    ExitFilled,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperFill {
    pub event_id: String,
    pub round_id: String,
    pub instrument_id: String,
    pub kind: PaperFillKind,
    pub ts_event: i64,
    #[serde(with = "serde_decimal_str")]
    pub quantity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub fee: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub realized_pnl: Option<DecimalStr>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperAccountState {
    #[serde(default = "default_currency")]
    pub currency: String,
    #[serde(with = "serde_decimal_str")]
    pub equity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub cash_available: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub margin_used: DecimalStr,
    pub trading_state: TradingState,
    #[serde(default)]
    pub trading_state_reason: Option<String>,
    #[serde(with = "serde_decimal_str")]
    pub intraday_drawdown_pct: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub peak_drawdown_pct: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub open_r: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub risk_reserved: DecimalStr,
    #[serde(default)]
    pub margin_mode: Option<MarginMode>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub leverage: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub r_unit_pct: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub max_single_r_pct: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub friction_buffer_pct: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub liquidation_buffer_pct: Option<DecimalStr>,
    #[serde(default)]
    pub liquidation_buffer_ticks: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperFeedStatus {
    #[serde(default = "default_true")]
    pub healthy: bool,
    #[serde(default)]
    pub reduce_reason: Option<String>,
    #[serde(default)]
    pub service_warning: Option<String>,
    #[serde(default)]
    pub updated_ts: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperProcessInfo {
    pub pid: u64,
    pub scheme: String,
    pub started_ts: i64,
    pub heartbeat_ts: i64,
    #[serde(default = "default_process_state")]
    pub status: PaperProcessState,
    #[serde(default)]
    pub stopped_ts: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperAccountView {
    pub sub_account_id: String,
    pub positions: Vec<PaperRound>,
    pub rounds: Vec<PaperRound>,
    pub fills: Vec<PaperFill>,
    #[serde(default)]
    pub ledger_seq: u64,
    #[serde(default)]
    pub account_state: Option<PaperAccountState>,
    #[serde(default)]
    pub feed_status: Option<PaperFeedStatus>,
    #[serde(default)]
    pub process: Option<PaperProcessInfo>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperCommandPayload {
    #[serde(default, with = "serde_decimal_str_opt")]
    pub stop: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_vec_opt")]
    pub take_profit: Option<Vec<DecimalStr>>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub ratio: Option<DecimalStr>,
    #[serde(default)]
    pub trading_state: Option<TradingState>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperCommand {
    pub command_id: String,
    pub sub_account_id: String,
    #[serde(default)]
    pub round_id: Option<String>,
    pub kind: PaperCommandKind,
    #[serde(default)]
    pub payload: PaperCommandPayload,
    pub based_on_ledger_seq: u64,
    pub issued_by: String,
    pub issued_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperCommandReceipt {
    pub command_id: String,
    #[serde(default)]
    pub sub_account_id: Option<String>,
    pub accepted: bool,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub ledger_seq: Option<u64>,
    #[serde(default)]
    pub processed_ts: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PaperCommandRecord {
    pub command: PaperCommand,
    #[serde(default)]
    pub claimed: bool,
    #[serde(default)]
    pub receipt: Option<PaperCommandReceipt>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SizingPreviewRequest {
    pub sub_account_id: String,
    pub instrument_id: String,
    pub direction: Direction,
    #[serde(with = "serde_decimal_str")]
    pub entry_low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub entry_high: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub invalidation_stop: DecimalStr,
    #[serde(default, with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    #[serde(default)]
    pub round_id: Option<String>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub override_quantity: Option<DecimalStr>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SizingPreviewResponse {
    #[serde(default, with = "serde_decimal_str_opt")]
    pub quantity: Option<DecimalStr>,
    #[serde(default)]
    pub basis: Option<String>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub risk_amount_r: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub risk_pct_of_equity: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub est_liq_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub est_liq_distance: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub est_liq_distance_pct: Option<DecimalStr>,
    #[serde(default)]
    pub rejected_reason: Option<String>,
    #[serde(default)]
    pub from_offline_snapshot: bool,
    #[serde(default)]
    pub based_on_ledger_seq: Option<u64>,
}
