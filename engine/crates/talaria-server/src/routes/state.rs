//! Watchlist, drawings, alerts, scanner/briefings, agent annotations, and collaboration endpoints.

use std::collections::HashSet;
use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{delete, get, patch, post, put};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use talaria_contracts::alerts::{
    AlertCreate, AlertListResponse, AlertPatch, AlertRecord, AlertStatus, AlertTriggerListResponse,
};
use talaria_contracts::collaboration::{
    AgentAnnotationCreate, AgentAnnotationRecord, AnnotationAdoptResponse,
    AnnotationAdoptionRecord, AnnotationClearRequest, AnnotationClearResponse,
    AnnotationKind, AnnotationListResponse, AnnotationSessionsResponse, AnnotationStatus,
    BriefingConsultRequest, BriefingConsultResponse, BriefingListResponse, BriefingResultRequest,
    BriefingStatus, ScannerBriefing,
};
use talaria_contracts::drawings::{
    ChartView, ChartViewWindow, DrawingClearResponse, DrawingCreate, DrawingKind,
    DrawingListResponse, DrawingPatch, DrawingPoint, DrawingRecord, DrawingStyle,
    DrawingVersionList,
};
use talaria_contracts::error_codes::{BRIEFING_NOT_FOUND, DRAWING_NOT_FOUND, FEATURE_DISABLED};
use talaria_contracts::feed::FeedHealthStatus;
use talaria_contracts::scanner::{
    ScannerConditionKind, ScannerFocusItem, ScannerFocusResponse, ScannerScanRequest,
    ScannerScanResponse, ScannerSeverity, ScannerTrigger, ScannerTriggerListResponse,
};
use talaria_contracts::watchlist::{
    DegradationReason, QuoteSource, Watchlist, WatchlistGroup, WatchlistGroupCreate,
    WatchlistGroupKind, WatchlistGroupUpdate, WatchlistItem, WatchlistItemCreate,
    WatchlistItemOrder, WatchlistOrder,
};
use uuid::Uuid;

use crate::error::ApiError;
use crate::error::{ApiJson, ApiPath, ApiQuery};
use crate::routes::find_instrument;
use crate::routes::validate_timeframe;
use crate::rules::{evaluate_annotation_gate, validate_rule_complexity};
use crate::server::AppState;
use crate::state_store::{
    SCANNER_FOCUS_GROUP_ID, alerts_get, alerts_list, alerts_list_triggers, alerts_soft_delete,
    alerts_write, annotations_clear, annotations_get, annotations_get_adoption, annotations_insert,
    annotations_list, annotations_list_sessions, annotations_record_adoption, annotations_update,
    briefings_list, briefings_mark_read, drawings_clear, drawings_create, drawings_get,
    drawings_list, drawings_list_versions, drawings_update, watchlist_add_item,
    watchlist_create_group, watchlist_delete_group, watchlist_list, watchlist_remove_item,
    watchlist_rename_group, watchlist_reorder_groups, watchlist_reorder_items,
};
use crate::subscriptions::WATCHLIST_TIMEFRAME;

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        // Watchlist
        .route("/api/watchlist", get(get_watchlist))
        .route("/api/watchlist/groups", post(create_watchlist_group))
        .route("/api/watchlist/groups/order", put(order_watchlist_groups))
        .route(
            "/api/watchlist/groups/{group_id}",
            patch(rename_watchlist_group).delete(delete_watchlist_group),
        )
        .route("/api/watchlist/items", post(add_watchlist_item))
        .route("/api/watchlist/items/order", put(order_watchlist_items))
        .route(
            "/api/watchlist/items/{instrument_id}",
            delete(remove_watchlist_item),
        )
        // Drawings
        .route(
            "/api/drawings",
            get(list_drawings).post(create_drawing).delete(clear_drawings),
        )
        .route(
            "/api/drawings/{drawing_id}",
            get(get_drawing).patch(patch_drawing).delete(delete_drawing),
        )
        .route("/api/drawings/{drawing_id}/versions", get(get_drawing_versions))
        .route("/api/drawings/{drawing_id}/restore", post(restore_drawing))
        .route("/api/chart-view", get(get_chart_view))
        // Alerts
        .route("/api/alerts", get(list_alerts).post(create_alert))
        .route("/api/alerts/triggers", get(list_alert_triggers))
        .route(
            "/api/alerts/{alert_id}",
            get(get_alert).patch(patch_alert).delete(delete_alert),
        )
        .route("/api/alerts/{alert_id}/confirm", post(confirm_alert))
        .route("/api/alerts/{alert_id}/dismiss", post(dismiss_alert))
        // Scanner & briefings
        .route("/api/scanner/triggers", get(list_scanner_triggers))
        .route("/api/scanner/focus", get(get_scanner_focus))
        .route("/api/scanner/scan", post(scan_now))
        .route("/api/scanner/briefings", get(list_scanner_briefings))
        .route(
            "/api/scanner/briefings/{briefing_id}/read",
            post(mark_scanner_briefing_read),
        )
        .route(
            "/api/scanner/briefings/{briefing_id}/consult",
            post(consult_scanner_briefing),
        )
        .route(
            "/api/scanner/briefings/{briefing_id}/result",
            post(submit_scanner_briefing_result),
        )
        // Annotations
        .route(
            "/api/annotations",
            post(create_annotation).get(list_annotations),
        )
        .route("/api/annotations/sessions", get(list_annotation_sessions))
        .route(
            "/api/annotations/{annotation_id}/adopt",
            post(adopt_annotation),
        )
        .route(
            "/api/annotations/{annotation_id}/dismiss",
            post(dismiss_annotation),
        )
        .route("/api/annotations/clear", post(clear_annotations))
        // Collaboration
        .route("/api/collaboration", get(collaboration_status))
}

// ============================================================================
// Watchlist
// ============================================================================

async fn build_watchlist_payload(state: &AppState) -> Result<Watchlist, ApiError> {
    let records = state.state_store.read(watchlist_list).await?;
    let now = state.now_ms();

    let mut ordered_manual_ids = Vec::new();
    let mut seen_ids = HashSet::new();
    for g in &records.groups {
        if !g.is_system {
            for item in records.items_for(&g.group_id) {
                if seen_ids.insert(item.instrument_id.clone()) {
                    ordered_manual_ids.push(item.instrument_id.clone());
                }
            }
        }
    }

    let active_alerts = state.alerts.active_alert_subscriptions();
    let sub_view = state
        .scheduler
        .reconcile(&ordered_manual_ids, Some(&active_alerts));

    let make_item = |instrument_id: &str, position: u32| -> WatchlistItem {
        let mut latest_price = None;
        let mut quote_ts = None;
        let mut quote_source = QuoteSource::Unavailable;

        let recent = state
            .hot_buffer
            .recent_bars(instrument_id, WATCHLIST_TIMEFRAME);
        if let Some(last) = recent.last() {
            latest_price = Some(last.close);
            quote_ts = Some(last.ts_event);
            quote_source = QuoteSource::Supervisor;
        }
        let feed_status = state
            .supervisor
            .as_ref()
            .and_then(|s| s.channel_health(instrument_id, WATCHLIST_TIMEFRAME))
            .map(|h| h.status);
        let realtime = sub_view.selected.contains(instrument_id);
        let stale = feed_status.is_some_and(|st| st != FeedHealthStatus::Live);
        let degradation_reason = if !state.has_supervisor() {
            Some(DegradationReason::NoFeed)
        } else {
            sub_view.degraded.get(instrument_id).copied()
        };
        let degraded = !realtime && degradation_reason.is_some();

        WatchlistItem {
            instrument_id: instrument_id.to_owned(),
            position,
            latest_price,
            change_pct: None,
            quote_ts,
            quote_source,
            realtime,
            feed_status,
            stale,
            degraded,
            degradation_reason,
        }
    };

    let mut groups = Vec::with_capacity(records.groups.len());
    for g in &records.groups {
        let kind = if g.kind == "SCANNER_FOCUS" {
            WatchlistGroupKind::ScannerFocus
        } else {
            WatchlistGroupKind::Manual
        };
        let items: Vec<WatchlistItem> = records
            .items_for(&g.group_id)
            .map(|row| make_item(&row.instrument_id, row.position as u32))
            .collect();
        groups.push(WatchlistGroup {
            group_id: g.group_id.clone(),
            name: g.name.clone(),
            position: g.position as u32,
            kind,
            is_system: g.is_system,
            items,
        });
    }

    Ok(Watchlist {
        groups,
        realtime_quota: sub_view.quota as u32,
        realtime_used: sub_view.used as u32,
        generated_ts: now,
    })
}

async fn get_watchlist(State(state): State<Arc<AppState>>) -> Result<ApiJson<Watchlist>, ApiError> {
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn create_watchlist_group(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<WatchlistGroupCreate>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    state
        .state_store
        .write(move |conn, now| {
            watchlist_create_group(conn, &payload.name, now)?;
            Ok(())
        })
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn order_watchlist_groups(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<WatchlistOrder>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    state
        .state_store
        .write(move |conn, now| watchlist_reorder_groups(conn, &payload.ids, now))
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn rename_watchlist_group(
    State(state): State<Arc<AppState>>,
    ApiPath(group_id): ApiPath<String>,
    ApiJson(payload): ApiJson<WatchlistGroupUpdate>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    state
        .state_store
        .write(move |conn, now| watchlist_rename_group(conn, &group_id, &payload.name, now))
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn delete_watchlist_group(
    State(state): State<Arc<AppState>>,
    ApiPath(group_id): ApiPath<String>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    state
        .state_store
        .write(move |conn, _| watchlist_delete_group(conn, &group_id))
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn add_watchlist_item(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<WatchlistItemCreate>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    let _ = find_instrument(&state, &payload.instrument_id)?;
    state
        .state_store
        .write(move |conn, now| {
            watchlist_add_item(conn, &payload.group_id, &payload.instrument_id, now)
        })
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn order_watchlist_items(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<WatchlistItemOrder>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    state
        .state_store
        .write(move |conn, now| {
            watchlist_reorder_items(conn, &payload.group_id, &payload.ids, now)
        })
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

async fn remove_watchlist_item(
    State(state): State<Arc<AppState>>,
    ApiPath(instrument_id): ApiPath<String>,
) -> Result<ApiJson<Watchlist>, ApiError> {
    state
        .state_store
        .write(move |conn, _| watchlist_remove_item(conn, &instrument_id))
        .await?;
    Ok(ApiJson(build_watchlist_payload(&state).await?))
}

// ============================================================================
// Drawings & Chart View
// ============================================================================

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListDrawingsQuery {
    instrument_id: String,
    timeframe: String,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    all_timeframes: bool,
    #[serde(default)]
    from_ts: Option<i64>,
    #[serde(default)]
    to_ts: Option<i64>,
}

async fn list_drawings(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ListDrawingsQuery>,
) -> Result<ApiJson<DrawingListResponse>, ApiError> {
    let _ = find_instrument(&state, &q.instrument_id)?;
    let _ = validate_timeframe(&q.timeframe)?;
    let inst = q.instrument_id.clone();
    let tf = q.timeframe.clone();
    let tf_filter = (!q.all_timeframes).then(|| q.timeframe.clone());

    let drawings = state
        .state_store
        .read(move |conn| {
            drawings_list(
                conn,
                &inst,
                tf_filter.as_deref(),
                q.from_ts,
                q.to_ts,
                None,
                None,
            )
        })
        .await?;

    Ok(ApiJson(DrawingListResponse {
        instrument_id: q.instrument_id,
        timeframe: tf,
        drawings,
    }))
}

fn validate_drawing_points(kind: DrawingKind, points: &[DrawingPoint]) -> Result<(), ApiError> {
    let expected = kind.expected_points();
    if points.len() != expected {
        return Err(ApiError::validation(format!(
            "{kind:?} 需要 {expected} 个锚点，收到 {} 个",
            points.len()
        )));
    }
    for p in points {
        if p.ts < 0 {
            return Err(ApiError::validation("point ts must be non-negative"));
        }
    }
    Ok(())
}

async fn create_drawing(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<DrawingCreate>,
) -> Result<(StatusCode, ApiJson<DrawingRecord>), ApiError> {
    let _ = find_instrument(&state, &payload.instrument_id)?;
    validate_drawing_points(payload.kind, &payload.points)?;
    for tf in &payload.visible_timeframes {
        let _ = validate_timeframe(tf)?;
    }
    let record = state
        .state_store
        .write(move |conn, now| {
            let rec = DrawingRecord {
                drawing_id: format!("drw_{}", Uuid::new_v4().simple()),
                instrument_id: payload.instrument_id,
                kind: payload.kind,
                visible_timeframes: payload.visible_timeframes,
                points: payload.points,
                text: payload.text,
                style: payload.style,
                version: 1,
                created_ts: now,
                updated_ts: now,
                deleted_at: None,
            };
            drawings_create(conn, &rec)?;
            Ok(rec)
        })
        .await?;
    state.alerts.refresh_drawings(std::slice::from_ref(&record));
    Ok((StatusCode::CREATED, ApiJson(record)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ClearDrawingsQuery {
    instrument_id: String,
}

async fn clear_drawings(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ClearDrawingsQuery>,
) -> Result<ApiJson<DrawingClearResponse>, ApiError> {
    let _ = find_instrument(&state, &q.instrument_id)?;
    let inst = q.instrument_id.clone();
    let (cleared, ids) = state
        .state_store
        .write(move |conn, now| {
            let existing = drawings_list(conn, &inst, None, None, None, None, None)?;
            let ids: Vec<String> = existing.into_iter().map(|d| d.drawing_id).collect();
            let count = drawings_clear(conn, &inst, now)?;
            Ok((count, ids))
        })
        .await?;
    state.alerts.drop_drawings(&ids);
    Ok(ApiJson(DrawingClearResponse {
        instrument_id: q.instrument_id,
        cleared,
    }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetDrawingQuery {
    #[serde(default)]
    version: Option<u64>,
}

async fn get_drawing(
    State(state): State<Arc<AppState>>,
    ApiPath(drawing_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<GetDrawingQuery>,
) -> Result<ApiJson<DrawingRecord>, ApiError> {
    let rec = state
        .state_store
        .read(move |conn| drawings_get(conn, &drawing_id, q.version))
        .await?;
    Ok(ApiJson(rec))
}

async fn get_drawing_versions(
    State(state): State<Arc<AppState>>,
    ApiPath(drawing_id): ApiPath<String>,
) -> Result<ApiJson<DrawingVersionList>, ApiError> {
    let id = drawing_id.clone();
    let versions = state
        .state_store
        .read(move |conn| drawings_list_versions(conn, &id))
        .await?;
    if versions.is_empty() {
        return Err(ApiError::not_found(
            DRAWING_NOT_FOUND,
            format!("画线 {drawing_id} 不存在"),
        ));
    }
    Ok(ApiJson(DrawingVersionList {
        drawing_id,
        versions,
    }))
}

async fn patch_drawing(
    State(state): State<Arc<AppState>>,
    ApiPath(drawing_id): ApiPath<String>,
    ApiJson(payload): ApiJson<DrawingPatch>,
) -> Result<ApiJson<DrawingRecord>, ApiError> {
    if let Some(tfs) = &payload.visible_timeframes {
        for tf in tfs {
            let _ = validate_timeframe(tf)?;
        }
    }
    let rec = state
        .state_store
        .write(move |conn, now| {
            let mut cur = drawings_get(conn, &drawing_id, None)?;
            if cur.deleted_at.is_some() {
                return Err(ApiError::not_found(
                    DRAWING_NOT_FOUND,
                    format!("画线 {drawing_id} 已删除"),
                ));
            }
            if let Some(pts) = payload.points {
                validate_drawing_points(cur.kind, &pts)?;
                cur.points = pts;
            }
            if let Some(tfs) = payload.visible_timeframes {
                cur.visible_timeframes = tfs;
            }
            if let Some(text) = payload.text {
                cur.text = text;
            }
            if let Some(style) = payload.style {
                cur.style = style;
            }
            cur.version += 1;
            cur.updated_ts = now;
            drawings_update(conn, &cur)?;
            Ok(cur)
        })
        .await?;
    state.alerts.refresh_drawings(std::slice::from_ref(&rec));
    Ok(ApiJson(rec))
}

async fn delete_drawing(
    State(state): State<Arc<AppState>>,
    ApiPath(drawing_id): ApiPath<String>,
) -> Result<ApiJson<DrawingRecord>, ApiError> {
    let rec = state
        .state_store
        .write(move |conn, now| {
            let mut cur = drawings_get(conn, &drawing_id, None)?;
            if cur.deleted_at.is_none() {
                cur.deleted_at = Some(now);
                cur.version += 1;
                cur.updated_ts = now;
                drawings_update(conn, &cur)?;
            }
            Ok(cur)
        })
        .await?;
    state.alerts.refresh_drawings(std::slice::from_ref(&rec));
    Ok(ApiJson(rec))
}

async fn restore_drawing(
    State(state): State<Arc<AppState>>,
    ApiPath(drawing_id): ApiPath<String>,
) -> Result<ApiJson<DrawingRecord>, ApiError> {
    let rec = state
        .state_store
        .write(move |conn, now| {
            let mut cur = drawings_get(conn, &drawing_id, None)?;
            if cur.deleted_at.is_some() {
                cur.deleted_at = None;
                cur.version += 1;
                cur.updated_ts = now;
                drawings_update(conn, &cur)?;
            }
            Ok(cur)
        })
        .await?;
    state.alerts.refresh_drawings(std::slice::from_ref(&rec));
    Ok(ApiJson(rec))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ChartViewQuery {
    instrument_id: String,
    timeframe: String,
    from_ts: i64,
    to_ts: i64,
    price_min: Decimal,
    price_max: Decimal,
}

async fn get_chart_view(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ChartViewQuery>,
) -> Result<ApiJson<ChartView>, ApiError> {
    let spec = find_instrument(&state, &q.instrument_id)?;
    let _ = validate_timeframe(&q.timeframe)?;
    if q.from_ts < 0 || q.to_ts < q.from_ts || q.price_max < q.price_min {
        return Err(ApiError::validation("invalid chart view bounds"));
    }
    let inst = q.instrument_id.clone();
    let tf = q.timeframe.clone();
    let drawings = state
        .state_store
        .read(move |conn| {
            drawings_list(
                conn,
                &inst,
                Some(&tf),
                Some(q.from_ts),
                Some(q.to_ts),
                Some(q.price_min),
                Some(q.price_max),
            )
        })
        .await?;

    Ok(ApiJson(ChartView {
        instrument: spec,
        timeframe: q.timeframe,
        window: ChartViewWindow {
            from_ts: q.from_ts,
            to_ts: q.to_ts,
            price_min: q.price_min,
            price_max: q.price_max,
        },
        drawings,
    }))
}

// ============================================================================
// Alerts
// ============================================================================

fn require_alerts_enabled(state: &AppState) -> Result<(), ApiError> {
    if !state.features.alerts {
        return Err(ApiError::conflict(FEATURE_DISABLED, "预警功能已关闭"));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListAlertsQuery {
    #[serde(default)]
    status: Option<AlertStatus>,
}

async fn list_alerts(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ListAlertsQuery>,
) -> Result<ApiJson<AlertListResponse>, ApiError> {
    require_alerts_enabled(&state)?;
    let changed = state.alerts.tick(None);
    if !changed.is_empty() {
        state
            .state_store
            .write(move |conn, _| {
                for a in &changed {
                    alerts_write(conn, a)?;
                }
                Ok(())
            })
            .await?;
    }
    let alerts = state
        .state_store
        .read(move |conn| alerts_list(conn, q.status))
        .await?;
    Ok(ApiJson(AlertListResponse { alerts }))
}

async fn create_alert(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<AlertCreate>,
) -> Result<(StatusCode, ApiJson<AlertRecord>), ApiError> {
    require_alerts_enabled(&state)?;
    let _ = find_instrument(&state, &payload.instrument_id)?;
    let _ = validate_timeframe(&payload.timeframe)?;
    validate_rule_complexity(&payload.condition)?;

    let status = if payload.draft {
        AlertStatus::Draft
    } else {
        AlertStatus::Armed
    };

    let record = state
        .state_store
        .write(move |conn, now| {
            let rec = AlertRecord {
                alert_id: format!("alt_{}", Uuid::new_v4().simple()),
                instrument_id: payload.instrument_id,
                timeframe: payload.timeframe,
                condition: payload.condition,
                status,
                repeat: payload.repeat,
                cooldown_ms: payload.cooldown_ms,
                expires_at: payload.expires_at,
                note: payload.note,
                created_ts: now,
                updated_ts: now,
                last_triggered_ts: None,
                next_arm_ts: None,
                trigger_count: 0,
            };
            alerts_write(conn, &rec)?;
            Ok(rec)
        })
        .await?;

    state.alerts.put(record.clone(), true)?;
    state.reconcile_watchlist_subscriptions();
    Ok((StatusCode::CREATED, ApiJson(record)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListTriggersQuery {
    #[serde(default)]
    alert_id: Option<String>,
    #[serde(default)]
    instrument_id: Option<String>,
    #[serde(default = "default_100")]
    limit: usize,
}

fn default_100() -> usize {
    100
}

async fn list_alert_triggers(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ListTriggersQuery>,
) -> Result<ApiJson<AlertTriggerListResponse>, ApiError> {
    require_alerts_enabled(&state)?;
    let triggers = state
        .state_store
        .read(move |conn| {
            alerts_list_triggers(
                conn,
                q.alert_id.as_deref(),
                q.instrument_id.as_deref(),
                q.limit,
            )
        })
        .await?;
    Ok(ApiJson(AlertTriggerListResponse { triggers }))
}

async fn get_alert(
    State(state): State<Arc<AppState>>,
    ApiPath(alert_id): ApiPath<String>,
) -> Result<ApiJson<AlertRecord>, ApiError> {
    require_alerts_enabled(&state)?;
    let rec = state
        .state_store
        .read(move |conn| alerts_get(conn, &alert_id))
        .await?;
    Ok(ApiJson(rec))
}

async fn patch_alert(
    State(state): State<Arc<AppState>>,
    ApiPath(alert_id): ApiPath<String>,
    ApiJson(payload): ApiJson<AlertPatch>,
) -> Result<ApiJson<AlertRecord>, ApiError> {
    require_alerts_enabled(&state)?;
    if let Some(cond) = &payload.condition {
        validate_rule_complexity(cond)?;
    }

    let rec = state
        .state_store
        .write(move |conn, now| {
            let mut cur = alerts_get(conn, &alert_id)?;
            if let Some(cond) = payload.condition {
                cur.condition = cond;
            }
            if let Some(note) = payload.note {
                cur.note = note;
            }
            if let Some(repeat) = payload.repeat {
                cur.repeat = repeat;
            }
            if let Some(cooldown_ms) = payload.cooldown_ms {
                cur.cooldown_ms = cooldown_ms;
            }
            if let Some(expires_at) = payload.expires_at {
                cur.expires_at = Some(expires_at);
            }
            if let Some(enabled) = payload.enabled {
                if enabled {
                    cur.status = AlertStatus::Armed;
                    cur.next_arm_ts = None;
                } else {
                    cur.status = AlertStatus::Disabled;
                    cur.next_arm_ts = None;
                }
            }
            cur.updated_ts = now;
            alerts_write(conn, &cur)?;
            Ok(cur)
        })
        .await?;

    state.alerts.put(rec.clone(), false)?;
    state.reconcile_watchlist_subscriptions();
    Ok(ApiJson(rec))
}

async fn delete_alert(
    State(state): State<Arc<AppState>>,
    ApiPath(alert_id): ApiPath<String>,
) -> Result<ApiJson<AlertRecord>, ApiError> {
    require_alerts_enabled(&state)?;
    let id_c = alert_id.clone();
    let rec = state
        .state_store
        .write(move |conn, now| alerts_soft_delete(conn, &id_c, now))
        .await?;
    state.alerts.remove(&alert_id);
    state.reconcile_watchlist_subscriptions();
    Ok(ApiJson(rec))
}

async fn confirm_alert(
    State(state): State<Arc<AppState>>,
    ApiPath(alert_id): ApiPath<String>,
) -> Result<ApiJson<AlertRecord>, ApiError> {
    require_alerts_enabled(&state)?;
    let rec = state
        .state_store
        .write(move |conn, now| {
            let mut cur = alerts_get(conn, &alert_id)?;
            if cur.status != AlertStatus::Draft {
                return Err(ApiError::validation(format!(
                    "只有 DRAFT 状态预警可以确认，当前 {:?}",
                    cur.status
                )));
            }
            cur.status = AlertStatus::Armed;
            cur.next_arm_ts = None;
            cur.updated_ts = now;
            alerts_write(conn, &cur)?;
            Ok(cur)
        })
        .await?;
    state.alerts.put(rec.clone(), true)?;
    state.reconcile_watchlist_subscriptions();
    Ok(ApiJson(rec))
}

async fn dismiss_alert(
    State(state): State<Arc<AppState>>,
    ApiPath(alert_id): ApiPath<String>,
) -> Result<ApiJson<AlertRecord>, ApiError> {
    patch_alert(
        State(state),
        ApiPath(alert_id),
        ApiJson(AlertPatch {
            enabled: Some(false),
            ..Default::default()
        }),
    )
    .await
}

// ============================================================================
// Scanner & Briefings
// ============================================================================

fn require_scanner_enabled(state: &AppState) -> Result<(), ApiError> {
    if !state.features.scanner {
        return Err(ApiError::conflict(FEATURE_DISABLED, "扫描功能已关闭"));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ScannerTriggersQuery {
    #[serde(default = "default_50")]
    limit: usize,
    #[serde(default)]
    instrument_id: Option<String>,
    #[serde(default)]
    kind: Option<ScannerConditionKind>,
    #[serde(default)]
    severity: Option<ScannerSeverity>,
    #[serde(default)]
    snapshot_id: Option<String>,
}

fn default_50() -> usize {
    50
}

async fn list_scanner_triggers(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ScannerTriggersQuery>,
) -> Result<ApiJson<ScannerTriggerListResponse>, ApiError> {
    require_scanner_enabled(&state)?;
    let all = state.scanner.list_triggers(q.instrument_id.as_deref(), 200);
    let filtered: Vec<ScannerTrigger> = all
        .into_iter()
        .filter(|t| {
            q.kind.is_none_or(|k| t.kind == k)
                && q.severity.is_none_or(|s| t.severity == s)
                && q.snapshot_id.as_deref().is_none_or(|sid| t.snapshot_id == sid)
        })
        .collect();
    let total = filtered.len();
    let triggers = filtered.into_iter().take(q.limit.clamp(1, 200)).collect();
    Ok(ApiJson(ScannerTriggerListResponse {
        triggers,
        total,
        generated_ts: state.now_ms(),
    }))
}

async fn get_scanner_focus(
    State(state): State<Arc<AppState>>,
) -> Result<ApiJson<ScannerFocusResponse>, ApiError> {
    require_scanner_enabled(&state)?;
    let now = state.now_ms();
    let focus_pairs = state.scanner.active_focus(Some(now));
    let all_triggers = state.scanner.list_triggers(None, 500);

    let mut items = Vec::with_capacity(focus_pairs.len());
    let mut inst_ids = Vec::with_capacity(focus_pairs.len());
    for (inst, last_ts) in focus_pairs {
        inst_ids.push(inst.clone());
        let matches: Vec<&ScannerTrigger> = all_triggers
            .iter()
            .filter(|t| t.instrument_id == inst)
            .collect();
        let mut kinds = Vec::new();
        let mut seen = HashSet::new();
        let mut max_sev = ScannerSeverity::Info;
        for m in &matches {
            if seen.insert(m.kind) {
                kinds.push(m.kind);
            }
            if severity_rank(m.severity) > severity_rank(max_sev) {
                max_sev = m.severity;
            }
        }
        let latest = matches.first();
        items.push(ScannerFocusItem {
            instrument_id: inst,
            last_trigger_ts: last_ts,
            trigger_count: matches.len(),
            severity: max_sev,
            kinds,
            last_title: latest.map(|t| t.title.clone()),
            last_timeframe: latest.map(|t| t.timeframe.clone()),
            last_snapshot_id: latest.map(|t| t.snapshot_id.clone()),
        });
    }

    Ok(ApiJson(ScannerFocusResponse {
        group_id: SCANNER_FOCUS_GROUP_ID.to_owned(),
        name: "异动焦点".to_owned(),
        items,
        instrument_ids: inst_ids,
        generated_ts: now,
    }))
}

fn severity_rank(s: ScannerSeverity) -> u8 {
    match s {
        ScannerSeverity::Info => 0,
        ScannerSeverity::Warning => 1,
        ScannerSeverity::Critical => 2,
    }
}

async fn scan_now(
    State(state): State<Arc<AppState>>,
    body: axum::body::Bytes,
) -> Result<ApiJson<ScannerScanResponse>, ApiError> {
    require_scanner_enabled(&state)?;
    let req: ScannerScanRequest = if body.is_empty() {
        ScannerScanRequest::default()
    } else {
        serde_json::from_slice(&body)
            .map_err(|e| ApiError::validation(format!("invalid JSON body: {e}")))?
    };
    let requested: Vec<String> = match req.instrument_ids {
        Some(ids) => {
            let mut seen = HashSet::new();
            let mut out = Vec::new();
            for id in ids {
                if seen.insert(id.clone()) {
                    let _ = find_instrument(&state, &id)?;
                    out.push(id);
                }
            }
            out
        }
        None => state
            .instruments_snapshot()
            .into_iter()
            .map(|s| s.instrument_id)
            .collect(),
    };

    let mut drawing_map = std::collections::HashMap::new();
    if let Some(rule) = &req.rule {
        let mut drw_ids = Vec::new();
        super::analysis::collect_rule_drawing_ids(&rule.expression, &mut drw_ids);
        for id in drw_ids {
            let id_c = id.clone();
            if let Ok(rec) = state
                .state_store
                .read(move |conn| drawings_get(conn, &id_c, None))
                .await
            {
                drawing_map.insert(id, rec);
            }
        }
    }

    let state_c = Arc::clone(&state);
    let req_c = requested.clone();
    let rule_c = req.rule.clone();

    let (triggers, _) = tokio::task::spawn_blocking(move || {
        let drawing_lookup = |id: &str| drawing_map.get(id).cloned();
        state_c
            .scanner
            .scan(Some(&req_c), None, rule_c.as_ref(), &drawing_lookup, false)
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    for trg in &triggers {
        state.desk.publish_scanner(trg);
    }

    // Sync scanner-focus system watchlist group
    let focus_ids: Vec<String> = state
        .scanner
        .active_focus(None)
        .into_iter()
        .map(|(id, _)| id)
        .collect();
    if !focus_ids.is_empty() {
        state
            .state_store
            .write(move |conn, now| {
                crate::state_store::watchlist_upsert_system_group(
                    conn,
                    SCANNER_FOCUS_GROUP_ID,
                    "异动焦点",
                    &focus_ids,
                    now,
                )
            })
            .await?;
    }

    Ok(ApiJson(ScannerScanResponse {
        triggers,
        scanned_instruments: requested,
        generated_ts: state.now_ms(),
    }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BriefingsQuery {
    #[serde(default = "default_50")]
    limit: usize,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    unread_only: bool,
}

async fn list_scanner_briefings(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<BriefingsQuery>,
) -> Result<ApiJson<BriefingListResponse>, ApiError> {
    let (briefings, unread) = state
        .state_store
        .read(move |conn| briefings_list(conn, q.limit, q.unread_only))
        .await?;
    Ok(ApiJson(BriefingListResponse {
        briefings,
        unread,
        generated_ts: state.now_ms(),
    }))
}

async fn mark_scanner_briefing_read(
    State(state): State<Arc<AppState>>,
    ApiPath(briefing_id): ApiPath<String>,
) -> Result<ApiJson<ScannerBriefing>, ApiError> {
    require_scanner_enabled(&state)?;
    let briefing = state
        .state_store
        .write(move |conn, now| briefings_mark_read(conn, &briefing_id, now))
        .await?;
    Ok(ApiJson(briefing))
}

async fn consult_scanner_briefing(
    State(state): State<Arc<AppState>>,
    ApiPath(briefing_id): ApiPath<String>,
    body: axum::body::Bytes,
) -> Result<ApiJson<BriefingConsultResponse>, ApiError> {
    require_scanner_enabled(&state)?;
    let payload: Option<BriefingConsultRequest> = if body.is_empty() {
        None
    } else {
        Some(
            serde_json::from_slice(&body)
                .map_err(|e| ApiError::validation(format!("invalid JSON body: {e}")))?,
        )
    };
    let custom_q = payload.and_then(|p| p.question);
    let briefing = state
        .state_store
        .read(move |conn| load_briefing_row(conn, &briefing_id))
        .await?;
    Ok(ApiJson(BriefingConsultResponse {
        briefing_id: briefing.id,
        snapshot_id: briefing.snapshot_id,
        instrument_id: briefing.instrument_id.clone(),
        timeframe: briefing.timeframe.clone(),
        question: custom_q.unwrap_or_else(|| {
            format!(
                "请分析 {} {} 扫描异动并提供关键位与风险。",
                briefing.instrument_id, briefing.timeframe
            )
        }),
    }))
}

async fn submit_scanner_briefing_result(
    State(state): State<Arc<AppState>>,
    ApiPath(briefing_id): ApiPath<String>,
    ApiJson(payload): ApiJson<BriefingResultRequest>,
) -> Result<ApiJson<ScannerBriefing>, ApiError> {
    require_scanner_enabled(&state)?;
    let updated = state
        .state_store
        .write(move |conn, _| {
            let mut cur = load_briefing_row(conn, &briefing_id)?;
            if let Some(content) = payload.content {
                cur.summary = Some(content);
                cur.status = BriefingStatus::Ready;
            } else if let Some(abstain) = payload.abstain {
                cur.abstain_reason = Some(abstain.message);
                cur.status = BriefingStatus::Abstain;
            } else if payload.error.is_some() {
                cur.status = BriefingStatus::Failed;
            }
            let status_str = serde_json::to_value(cur.status)
                .ok()
                .and_then(|v| v.as_str().map(str::to_owned))
                .unwrap_or_else(|| "READY".to_owned());
            let encoded = serde_json::to_string(&cur).map_err(|e| ApiError::internal(e.to_string()))?;
            conn.execute(
                "UPDATE scanner_briefings SET status = ?1, payload = ?2 WHERE briefing_id = ?3",
                rusqlite::params![status_str, encoded, briefing_id],
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
            Ok(cur)
        })
        .await?;
    state.desk.publish_briefing(&updated);
    Ok(ApiJson(updated))
}

fn load_briefing_row(
    conn: &rusqlite::Connection,
    briefing_id: &str,
) -> Result<ScannerBriefing, ApiError> {
    use rusqlite::OptionalExtension;
    let raw: Option<String> = conn
        .query_row(
            "SELECT payload FROM scanner_briefings WHERE briefing_id = ?1",
            rusqlite::params![briefing_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let Some(raw) = raw else {
        return Err(ApiError::not_found(
            BRIEFING_NOT_FOUND,
            format!("简报 {briefing_id} 不存在"),
        ));
    };
    serde_json::from_str(&raw)
        .map_err(|e| ApiError::internal(format!("corrupt briefing payload: {e}")))
}

// ============================================================================
// Annotations
// ============================================================================

async fn create_annotation(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<AgentAnnotationCreate>,
) -> Result<(StatusCode, ApiJson<AgentAnnotationRecord>), ApiError> {
    let spec = find_instrument(&state, &payload.instrument_id)?;
    let _ = validate_timeframe(&payload.timeframe)?;

    let state_c = Arc::clone(&state);
    let snap_id = payload.snapshot_id.clone();
    let tf = payload.timeframe.clone();
    let payload_c = payload.clone();

    tokio::task::spawn_blocking(move || -> Result<(), ApiError> {
        let (meta, bars) = state_c.snapshots.get_bars(&snap_id, &tf)?;
        let valid_levels: HashSet<String> = state_c
            .key_levels
            .lookup_level_map(&meta.instrument_id, &tf, meta.cutoff_ts)
            .into_keys()
            .collect();
        evaluate_annotation_gate(&payload_c, &spec, &meta, &bars, &valid_levels)
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    let record = state
        .state_store
        .write(move |conn, now| {
            let rec = AgentAnnotationRecord {
                annotation_id: format!("ann_{}", Uuid::new_v4().simple()),
                snapshot_id: payload.snapshot_id,
                instrument_id: payload.instrument_id,
                timeframe: payload.timeframe,
                session_id: payload.session_id,
                round: payload.round,
                kind: payload.kind,
                status: AnnotationStatus::Active,
                label: payload.label,
                rationale: payload.rationale,
                price_low: payload.price_low,
                price_high: payload.price_high,
                ts_anchor: payload.ts_anchor,
                evidence_refs: payload.evidence_refs,
                created_ts: now,
                updated_ts: now,
                adopted_drawing_id: None,
                archived_reason: None,
            };
            annotations_insert(conn, &rec, 50)?;
            Ok(rec)
        })
        .await?;

    Ok((StatusCode::CREATED, ApiJson(record)))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListAnnotationsQuery {
    #[serde(default)]
    instrument_id: Option<String>,
    #[serde(default)]
    snapshot_id: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    include_archived: bool,
}

async fn list_annotations(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<ListAnnotationsQuery>,
) -> Result<ApiJson<AnnotationListResponse>, ApiError> {
    let annotations = state
        .state_store
        .read(move |conn| {
            annotations_list(
                conn,
                q.instrument_id.as_deref(),
                q.snapshot_id.as_deref(),
                q.session_id.as_deref(),
                q.include_archived,
            )
        })
        .await?;
    Ok(ApiJson(AnnotationListResponse { annotations }))
}

async fn list_annotation_sessions(
    State(state): State<Arc<AppState>>,
) -> Result<ApiJson<AnnotationSessionsResponse>, ApiError> {
    let sessions = state
        .state_store
        .read(annotations_list_sessions)
        .await?;
    Ok(ApiJson(AnnotationSessionsResponse { sessions }))
}

async fn adopt_annotation(
    State(state): State<Arc<AppState>>,
    ApiPath(annotation_id): ApiPath<String>,
) -> Result<ApiJson<AnnotationAdoptResponse>, ApiError> {
    let res = state
        .state_store
        .write(move |conn, now| {
            let mut ann = annotations_get(conn, &annotation_id)?;
            if let Some(existing) = annotations_get_adoption(conn, &annotation_id)?
                && drawings_get(conn, &existing.drawing_id, None)
                    .ok()
                    .is_some_and(|d| d.deleted_at.is_none())
            {
                return Ok(AnnotationAdoptResponse {
                    annotation: ann,
                    adoption: existing,
                });
            }

            let (kind, points) = match ann.kind {
                AnnotationKind::Level => (
                    DrawingKind::HorizontalLine,
                    vec![DrawingPoint {
                        ts: ann.ts_anchor,
                        price: ann.price_low,
                    }],
                ),
                AnnotationKind::Zone => (
                    DrawingKind::Rectangle,
                    vec![
                        DrawingPoint {
                            ts: ann.ts_anchor,
                            price: ann.price_low,
                        },
                        DrawingPoint {
                            ts: ann.ts_anchor + 60_000,
                            price: ann.price_high,
                        },
                    ],
                ),
            };

            let drawing = DrawingRecord {
                drawing_id: format!("drw_{}", Uuid::new_v4().simple()),
                instrument_id: ann.instrument_id.clone(),
                kind,
                visible_timeframes: vec![ann.timeframe.clone()],
                points,
                text: ann.label.clone(),
                style: DrawingStyle::default(),
                version: 1,
                created_ts: now,
                updated_ts: now,
                deleted_at: None,
            };
            drawings_create(conn, &drawing)?;

            ann.status = AnnotationStatus::Adopted;
            ann.adopted_drawing_id = Some(drawing.drawing_id.clone());
            ann.updated_ts = now;
            annotations_update(conn, &ann)?;

            let adoption = AnnotationAdoptionRecord {
                annotation_id: ann.annotation_id.clone(),
                instrument_id: ann.instrument_id.clone(),
                snapshot_id: ann.snapshot_id.clone(),
                timeframe: ann.timeframe.clone(),
                session_id: ann.session_id.clone(),
                round: ann.round,
                drawing_id: drawing.drawing_id,
                adopted_ts: now,
            };
            annotations_record_adoption(conn, &adoption)?;

            Ok(AnnotationAdoptResponse {
                annotation: ann,
                adoption,
            })
        })
        .await?;

    Ok(ApiJson(res))
}

async fn dismiss_annotation(
    State(state): State<Arc<AppState>>,
    ApiPath(annotation_id): ApiPath<String>,
) -> Result<ApiJson<AgentAnnotationRecord>, ApiError> {
    let rec = state
        .state_store
        .write(move |conn, now| {
            let mut ann = annotations_get(conn, &annotation_id)?;
            ann.status = AnnotationStatus::Dismissed;
            ann.updated_ts = now;
            annotations_update(conn, &ann)?;
            Ok(ann)
        })
        .await?;
    Ok(ApiJson(rec))
}

async fn clear_annotations(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<AnnotationClearRequest>,
) -> Result<ApiJson<AnnotationClearResponse>, ApiError> {
    let _ = find_instrument(&state, &payload.instrument_id)?;
    let inst = payload.instrument_id.clone();
    let sess = payload.session_id.clone();
    let cleared = state
        .state_store
        .write(move |conn, now| annotations_clear(conn, &inst, sess.as_deref(), now))
        .await?;
    Ok(ApiJson(AnnotationClearResponse {
        instrument_id: payload.instrument_id,
        session_id: payload.session_id,
        cleared,
    }))
}

// ============================================================================
// Collaboration
// ============================================================================

#[derive(Serialize)]
struct CollaborationStatus {
    mode: &'static str,
    host: &'static str,
    url: Option<String>,
    detail: &'static str,
    message: &'static str,
}

async fn collaboration_status() -> ApiJson<CollaborationStatus> {
    ApiJson(CollaborationStatus {
        mode: "solo",
        host: "disabled",
        url: None,
        detail: "单机模式未启动 DSH 协同宿主",
        message: "单机模式：图表由 Rust 守护进程直接提供。",
    })
}
