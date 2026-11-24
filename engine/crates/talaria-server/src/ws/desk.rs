//! Fast-path workbench WebSocket hub (`WS /ws/desk`).
//!
//! Bounded 256-frame per-client ring queue: drops oldest frames on overflow
//! so slow desk UI consumers never stall live broadcasts.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use axum::extract::State;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::response::IntoResponse;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use talaria_contracts::alerts::AlertTriggerRecord;
use talaria_contracts::collaboration::ScannerBriefing;
use talaria_contracts::error_codes::{INSTRUMENT_NOT_FOUND, VALIDATION_ERROR};
use crate::error::SUBSCRIPTION_QUOTA;
use talaria_contracts::feed::FeedEvent;
use talaria_contracts::key_levels::KeyLevelSet;
use talaria_contracts::scanner::ScannerTrigger;
use talaria_contracts::{SUPPORTED_TIMEFRAMES};
use talaria_feed::FeedSupervisor;
use tokio::sync::Notify;

use crate::key_levels::KeyLevelRegistry;
use crate::server::AppState;
use crate::subscriptions::{Owner, OwnerKind, SubscriptionKey, SubscriptionScheduler};

pub const WS_QUEUE_MAX: usize = 256;

#[derive(Clone)]
pub struct DeskQueue {
    inner: Arc<Mutex<VecDeque<String>>>,
    notify: Arc<Notify>,
}

impl Default for DeskQueue {
    fn default() -> Self {
        Self::new()
    }
}

impl DeskQueue {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(VecDeque::with_capacity(WS_QUEUE_MAX))),
            notify: Arc::new(Notify::new()),
        }
    }

    pub fn push_drop_oldest(&self, payload: String) -> bool {
        let mut dropped = false;
        {
            let mut q = self.inner.lock().expect("desk queue poisoned");
            if q.len() >= WS_QUEUE_MAX {
                q.pop_front();
                dropped = true;
            }
            q.push_back(payload);
        }
        self.notify.notify_one();
        dropped
    }

    pub async fn recv(&self) -> String {
        loop {
            if let Some(msg) = {
                let mut q = self.inner.lock().expect("desk queue poisoned");
                q.pop_front()
            } {
                return msg;
            }
            self.notify.notified().await;
        }
    }

    pub fn len(&self) -> usize {
        self.inner.lock().expect("desk queue poisoned").len()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.lock().expect("desk queue poisoned").is_empty()
    }
}

struct ClientSlot {
    queue: DeskQueue,
    filters: HashSet<SubscriptionKey>,
}

struct BroadcastInner {
    next_id: u64,
    clients: HashMap<u64, ClientSlot>,
    chart_subscribers: HashMap<SubscriptionKey, usize>,
}

/// Shared non-blocking fanout hub for `/ws/desk`.
pub struct DeskBroadcast {
    next_seq: AtomicU64,
    inner: Mutex<BroadcastInner>,
}

impl Default for DeskBroadcast {
    fn default() -> Self {
        Self::new()
    }
}

impl DeskBroadcast {
    pub fn new() -> Self {
        Self {
            next_seq: AtomicU64::new(1),
            inner: Mutex::new(BroadcastInner {
                next_id: 1,
                clients: HashMap::new(),
                chart_subscribers: HashMap::new(),
            }),
        }
    }

    pub fn attach(&self) -> (u64, DeskQueue) {
        let queue = DeskQueue::new();
        let mut guard = self.inner.lock().expect("desk hub poisoned");
        let id = guard.next_id;
        guard.next_id += 1;
        guard.clients.insert(
            id,
            ClientSlot {
                queue: queue.clone(),
                filters: HashSet::new(),
            },
        );
        (id, queue)
    }

    pub fn detach(&self, client_id: u64) -> Vec<SubscriptionKey> {
        let mut guard = self.inner.lock().expect("desk hub poisoned");
        let removed = guard
            .clients
            .remove(&client_id)
            .map(|c| c.filters.into_iter().collect::<Vec<_>>())
            .unwrap_or_default();
        for key in &removed {
            decrement_retainer(&mut guard.chart_subscribers, key);
        }
        removed
    }

    pub fn subscribe_client(&self, client_id: u64, key: SubscriptionKey) -> bool {
        let mut guard = self.inner.lock().expect("desk hub poisoned");
        let inserted = match guard.clients.get_mut(&client_id) {
            Some(slot) => slot.filters.insert(key.clone()),
            None => false,
        };
        if inserted {
            *guard.chart_subscribers.entry(key).or_insert(0) += 1;
        }
        inserted
    }

    pub fn unsubscribe_client(&self, client_id: u64, key: &SubscriptionKey) -> bool {
        let mut guard = self.inner.lock().expect("desk hub poisoned");
        let removed = match guard.clients.get_mut(&client_id) {
            Some(slot) => slot.filters.remove(key),
            None => false,
        };
        if removed {
            decrement_retainer(&mut guard.chart_subscribers, key);
        }
        removed
    }

    pub fn chart_subscriber_count(&self, key: &SubscriptionKey) -> usize {
        self.inner
            .lock()
            .expect("desk hub poisoned")
            .chart_subscribers
            .get(key)
            .copied()
            .unwrap_or(0)
    }

    pub fn publish_feed_event(&self, event: &FeedEvent) {
        let Some((msg_type, json_data)) = feed_event_payload(event) else {
            return;
        };
        let payload = format!(r#"{{"type":"{msg_type}","data":{json_data}}}"#);
        let guard = self.inner.lock().expect("desk hub poisoned");
        for slot in guard.clients.values() {
            if matches_filters(event, &slot.filters) {
                slot.queue.push_drop_oldest(payload.clone());
            }
        }
        let _ = self.next_seq.fetch_add(1, Ordering::Relaxed);
    }

    pub fn project_levels(&self, level_set: &KeyLevelSet) {
        let Ok(data) = serde_json::to_string(level_set) else {
            return;
        };
        let payload = format!(r#"{{"type":"levels","data":{data}}}"#);
        let key = (level_set.instrument_id.clone(), level_set.timeframe.clone());
        let guard = self.inner.lock().expect("desk hub poisoned");
        for slot in guard.clients.values() {
            if slot.filters.contains(&key) {
                slot.queue.push_drop_oldest(payload.clone());
            }
        }
    }

    pub fn publish_alert(&self, trigger: &AlertTriggerRecord) {
        if let Ok(data) = serde_json::to_string(trigger) {
            self.broadcast(&format!(r#"{{"type":"alert","data":{data}}}"#));
        }
    }

    pub fn publish_alert_quota(&self, denied_alerts: &[String]) {
        if let Ok(json) = serde_json::to_string(&serde_json::json!({
            "type": "alert_quota",
            "denied_alerts": denied_alerts,
        })) {
            self.broadcast(&json);
        }
    }

    pub fn publish_subscription_error(&self, owner: &Owner) {
        let (kind, owner_id, (instrument_id, timeframe)) = owner;
        let (kind_str, label) = match kind {
            OwnerKind::Alert => ("alert", format!("预警 {owner_id}")),
            OwnerKind::Watchlist => ("watchlist", "自选".to_owned()),
            OwnerKind::Chart => ("chart", format!("图表 {owner_id}")),
        };
        if let Ok(json) = serde_json::to_string(&serde_json::json!({
            "type": "error",
            "code": SUBSCRIPTION_QUOTA,
            "message": format!("{label}实时订阅配额不足：{instrument_id} {timeframe}"),
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "owner": kind_str,
            "owner_id": owner_id,
        })) {
            self.broadcast(&json);
        }
    }

    pub fn publish_scanner(&self, trigger: &ScannerTrigger) {
        if let Ok(data) = serde_json::to_string(trigger) {
            self.broadcast(&format!(r#"{{"type":"scanner","data":{data}}}"#));
        }
    }

    pub fn publish_briefing(&self, briefing: &ScannerBriefing) {
        if let Ok(data) = serde_json::to_string(briefing) {
            self.broadcast(&format!(r#"{{"type":"briefing","data":{data}}}"#));
        }
    }

    pub fn broadcast(&self, payload: &str) {
        let guard = self.inner.lock().expect("desk hub poisoned");
        for slot in guard.clients.values() {
            slot.queue.push_drop_oldest(payload.to_owned());
        }
    }
}

fn decrement_retainer(map: &mut HashMap<SubscriptionKey, usize>, key: &SubscriptionKey) {
    if let Some(count) = map.get_mut(key) {
        if *count <= 1 {
            map.remove(key);
        } else {
            *count -= 1;
        }
    }
}

fn matches_filters(event: &FeedEvent, filters: &HashSet<SubscriptionKey>) -> bool {
    match event {
        FeedEvent::Kline(k) => filters.contains(&(k.instrument_id.clone(), k.timeframe.clone())),
        FeedEvent::Health(h) => filters.contains(&(h.instrument_id.clone(), h.timeframe.clone())),
        FeedEvent::OrderBook(b) => filters.iter().any(|(inst, _)| inst == &b.instrument_id),
        FeedEvent::DerivativeTick(d) => filters.iter().any(|(inst, _)| inst == &d.instrument_id),
        FeedEvent::Trade(_) => false,
    }
}

pub fn feed_event_payload(event: &FeedEvent) -> Option<(&'static str, String)> {
    match event {
        FeedEvent::Kline(k) => serde_json::to_string(k).ok().map(|s| ("bar", s)),
        FeedEvent::OrderBook(b) => serde_json::to_string(b).ok().map(|s| ("book", s)),
        FeedEvent::DerivativeTick(d) => serde_json::to_string(d).ok().map(|s| ("derivatives", s)),
        FeedEvent::Health(h) => serde_json::to_string(h).ok().map(|s| ("health", s)),
        FeedEvent::Trade(_) => None,
    }
}

#[derive(Deserialize)]
struct ClientCommand {
    #[serde(default)]
    op: String,
    #[serde(default)]
    instrument_id: String,
    #[serde(default)]
    timeframe: String,
}

#[derive(Serialize)]
struct ErrorFrame<'a> {
    #[serde(rename = "type")]
    msg_type: &'static str,
    code: &'a str,
    message: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    owner: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    instrument_id: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    timeframe: Option<&'a str>,
}

pub async fn ws_desk_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_desk_socket(socket, state))
}

async fn handle_desk_socket(socket: WebSocket, state: Arc<AppState>) {
    let (mut sink, mut stream) = socket.split();

    let (client_id, queue) = state.desk.attach();
    let denied = state.scheduler.denied_alerts();
    let hello = serde_json::json!({
        "type": "hello",
        "denied_alerts": denied,
        "feed_mode": state.feed_mode_str(),
        "server_ts": state.now_ms(),
        "live": state.has_supervisor(),
    });
    if sink
        .send(Message::Text(hello.to_string().into()))
        .await
        .is_err()
    {
        cleanup_client(&state, client_id);
        return;
    }

    let current_denied = state.scheduler.denied_alerts();
    if current_denied != denied
        && let Ok(msg) = serde_json::to_string(&serde_json::json!({
            "type": "alert_quota",
            "denied_alerts": current_denied,
        }))
    {
        queue.push_drop_oldest(msg);
    }

    let send_queue = queue.clone();
    let mut send_task = tokio::spawn(async move {
        loop {
            let text = send_queue.recv().await;
            if sink.send(Message::Text(text.into())).await.is_err() {
                break;
            }
        }
    });

    let valid_instruments: HashSet<String> = state
        .instruments
        .iter()
        .map(|s| s.instrument_id.clone())
        .collect();

    loop {
        tokio::select! {
            _ = &mut send_task => {
                break;
            }
            incoming = stream.next() => {
                match incoming {
                    Some(Ok(Message::Text(text))) => {
                        let Ok(cmd) = serde_json::from_str::<ClientCommand>(&text) else {
                            continue;
                        };
                        handle_desk_command(
                            &cmd,
                            client_id,
                            &queue,
                            &state.desk,
                            &state.scheduler,
                            state.supervisor.as_ref(),
                            &state.key_levels,
                            &valid_instruments,
                            &state,
                        );
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {}
                    Some(Err(_)) => break,
                }
            }
        }
    }

    send_task.abort();
    cleanup_client(&state, client_id);
}

#[allow(clippy::too_many_arguments)]
fn handle_desk_command(
    cmd: &ClientCommand,
    client_id: u64,
    queue: &DeskQueue,
    desk: &DeskBroadcast,
    scheduler: &SubscriptionScheduler,
    supervisor: Option<&Arc<FeedSupervisor>>,
    key_levels: &KeyLevelRegistry,
    valid_instruments: &HashSet<String>,
    state: &AppState,
) {
    let key = (cmd.instrument_id.clone(), cmd.timeframe.clone());
    match cmd.op.as_str() {
        "subscribe" => {
            if !valid_instruments.contains(&cmd.instrument_id) {
                let msg = format!("未知标的：{}", cmd.instrument_id);
                push_error(queue, INSTRUMENT_NOT_FOUND, &msg, None, None, None);
                return;
            }
            if !SUPPORTED_TIMEFRAMES.contains(&cmd.timeframe.as_str()) {
                let msg = format!("不支持的周期：{}", cmd.timeframe);
                push_error(queue, VALIDATION_ERROR, &msg, None, None, None);
                return;
            }
            if !desk.subscribe_client(client_id, key.clone()) {
                return;
            }
            let had_channel = supervisor
                .map(|s| s.has_channel(&cmd.instrument_id, &cmd.timeframe))
                .unwrap_or(false);
            if let Err(err) = scheduler.acquire_chart(key.clone()) {
                desk.unsubscribe_client(client_id, &key);
                push_error(
                    queue,
                    err.code,
                    &err.message,
                    Some("chart"),
                    Some(&cmd.instrument_id),
                    Some(&cmd.timeframe),
                );
                return;
            }
            state.reconcile_watchlist_subscriptions();
            if had_channel
                && let Some(sup) = supervisor
                && let Some(health) = sup.channel_health(&cmd.instrument_id, &cmd.timeframe)
                && let Some((ty, data)) = feed_event_payload(&FeedEvent::Health(health))
            {
                queue.push_drop_oldest(format!(r#"{{"type":"{ty}","data":{data}}}"#));
            }
            if let Some(latest) = key_levels.latest(&cmd.instrument_id, &cmd.timeframe)
                && let Ok(data) = serde_json::to_string(&latest)
            {
                queue.push_drop_oldest(format!(r#"{{"type":"levels","data":{data}}}"#));
            }
        }
        "unsubscribe" => {
            if desk.unsubscribe_client(client_id, &key) {
                scheduler.release_chart(&key);
                if desk.chart_subscriber_count(&key) == 0 {
                    state.reconcile_watchlist_subscriptions();
                }
            }
        }
        _ => {
            push_error(queue, VALIDATION_ERROR, "未知的订阅操作", None, None, None);
        }
    }
}

fn push_error(
    queue: &DeskQueue,
    code: &str,
    message: &str,
    owner: Option<&'static str>,
    instrument_id: Option<&str>,
    timeframe: Option<&str>,
) {
    if let Ok(json) = serde_json::to_string(&ErrorFrame {
        msg_type: "error",
        code,
        message,
        owner,
        instrument_id,
        timeframe,
    }) {
        queue.push_drop_oldest(json);
    }
}

fn cleanup_client(state: &AppState, client_id: u64) {
    let removed = state.desk.detach(client_id);
    for key in removed {
        state.scheduler.release_chart(&key);
    }
    state.reconcile_watchlist_subscriptions();
}
