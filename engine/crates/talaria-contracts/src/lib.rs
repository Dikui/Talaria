//! Zero-I/O L0 domain contracts, fixed-point `DecimalStr` adapters, 5-timestamp provenance,
//! `InstrumentSpec`, Capability Traits, bars, ticks, derivatives, events, key levels, indicators,
//! drawings, rules, alerts, scanner, and collaboration payloads.

pub mod alerts;
pub mod bars;
pub mod collaboration;
pub mod decimal;
pub mod derivatives;
pub mod drawings;
pub mod error;
pub mod feed;
pub mod indicators;
pub mod key_levels;
pub mod market;
pub mod rules;
pub mod runs;
pub mod scanner;
pub mod snapshot;
pub mod strategy_subscription;
pub mod ticks;
pub mod timeframes;
pub mod types;
pub mod watchlist;

pub use alerts::{
    AlertCreate, AlertListResponse, AlertPatch, AlertRecord, AlertStatus, AlertTriggerKind,
    AlertTriggerListResponse, AlertTriggerRecord,
};
pub use bars::{
    BarStatus, BarsQuery, BarsResponse, CompactBar, MAX_RANGE_BARS, RangeBar, RawBar,
    classify_flat_bar,
};
pub use collaboration::{
    Abstain, AbstainReason, AgentAnnotationCreate, AgentAnnotationRecord, AnnotationAdoptResponse,
    AnnotationAdoptionRecord, AnnotationClearRequest, AnnotationClearResponse, AnnotationEvidence,
    AnnotationKind, AnnotationListResponse, AnnotationSessionSummary, AnnotationSessionsResponse,
    AnnotationStatus, BriefingConsultRequest, BriefingConsultResponse, BriefingContent,
    BriefingLevelRef, BriefingListResponse, BriefingResultRequest, BriefingStatus, CatalystEvent,
    CollabWindow, ConfirmedDraft, ConsultRequest, ConsultResponse, ConsultationListResponse,
    ConsultationPackage, ConsultationRecord, ConsultationStatus, DraftConfirmation, DraftContent,
    DraftDeliveryRequest, DraftEdit, DraftEvidence, DraftReceipt, DraftRecord, DraftStatus,
    DrawingRef, EvidenceKind, EvidenceRef, ExitBarrierSpec, HostResultRequest, PaperAccountView,
    PaperFill, PaperFillKind, PaperRound, ScannerBriefing, SetupDirection, SetupInsight,
    TrailingStopSpec, UsageReport, UsageReportResponse, UsageRow, UsageSummary,
    UsageSummaryResponse,
};
pub use decimal::{
    DECIMAL_PATTERN, DecimalStr, format_decimal_str, format_normalized_decimal_str,
    parse_decimal_str, validate_decimal_str,
};
pub use derivatives::{
    DerivativeBundle, DerivativeKind, DerivativePoint, DerivativeSeries, DerivativeSummary,
    RawDerivativePoint, latest_visible_derivatives, oi_timeframe_for,
};
pub use drawings::{
    ChartView, ChartViewWindow, DrawingClearResponse, DrawingCreate, DrawingKind,
    DrawingLineStyle, DrawingListResponse, DrawingPatch, DrawingPoint, DrawingRecord,
    DrawingStyle, DrawingVersionList,
};
pub use error::{ContractError, error_codes};
pub use feed::{
    DerivativeTickEvent, FeedEvent, FeedHealthEvent, FeedHealthReport, FeedHealthStatus,
    KlineEvent, OrderBookEvent, ReceivedKlineEvent, ReceivedOrderBookEvent, TradeEvent,
    TransportEvent, worst_status,
};
pub use indicators::{
    IndicatorBundle, IndicatorDefaultsResponse, IndicatorName, IndicatorPane, IndicatorParams,
    IndicatorPoint, IndicatorRequest, IndicatorSeries, IndicatorSummary, MAX_INDICATOR_PERIOD,
    OperatorRequest, VwapAnchorMode,
};
pub use key_levels::{
    EvidenceClass, KEY_LEVEL_METHOD_VERSION, KeyLevel, KeyLevelAdoptionRecord,
    KeyLevelAdoptionRequest, KeyLevelCategory, KeyLevelFeedbackSummary, KeyLevelIgnoreList,
    KeyLevelIgnoreRecord, KeyLevelIgnoreRequest, KeyLevelKind, KeyLevelSet, KeyLevelStatus,
    SessionProfile, VwapAnchor, kind_category,
};
pub use market::{
    CarryTrait, DerivativeCapabilityTrait, Direction, FeedCapabilityTrait, InstrumentSpec,
    MarginEstimate, MarginTrait, OrderConstraintTrait, SessionTrait, TradingSession, TraitBundle,
};
pub use rules::{
    AlertDirection, AlertLevelEvent, AlertMetric, AlertOverlayMode, MAX_RULE_DEPTH, MAX_RULE_NODES,
    Rule, RuleBarField, RuleComparisonTest, RuleEdge, RuleExpression, RuleIndicatorName,
    RuleIndicatorParams, RuleIndicatorWindow, RuleOperand, RulePane, RulePreviewIndicator,
    RulePreviewRequest, RulePreviewResponse, RulePreviewSignal, RuleTiming, RuleVersion,
};
pub use scanner::{
    ScannerConditionKind, ScannerEvidence, ScannerFocusItem, ScannerFocusResponse,
    ScannerScanRequest, ScannerScanResponse, ScannerSeverity, ScannerTrigger,
    ScannerTriggerListResponse, ScannerTriggerRoute,
};
pub use snapshot::{ChartPage, ErrorResponse, SnapshotMeta, SnapshotMode, SnapshotRequest};
pub use strategy_subscription::{
    StrategyBarEvent, StrategyDerivativeEvent, SubscriptionCursor, SubscriptionReady,
    SubscriptionReadyType, SubscriptionRequest,
};
pub use ticks::{Quote, ReplayTick, TickKind, TicksJsonResponse, Trade, TradeAvailability, TradeSide};
pub use runs::{
    RunAccountCurrencySummary, RunBarrierChange, RunBarrierChangeKind, RunEquityPoint,
    RunEquityResponse, RunExportArtifacts, RunExportSummary, RunFillKind, RunFillReason,
    RunFillSide, RunFillTier, RunListResponse, RunRoundFill, RunRoundPhase, RunRoundRecord,
    RunRoundsResponse, RunStatus,
};
pub use timeframes::{SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms};
pub use types::{PrecisionTier, TemporalStamp};
pub use watchlist::{
    DegradationReason, QuoteSource, Watchlist, WatchlistGroup, WatchlistGroupCreate,
    WatchlistGroupKind, WatchlistGroupUpdate, WatchlistItem, WatchlistItemCreate,
    WatchlistItemOrder, WatchlistOrder,
};
