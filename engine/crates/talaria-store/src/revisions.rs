//! Shared Point-in-Time (PIT) cutoff selection and cold/hot revision reconciliation (ADR-0018).

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::hash::Hash;

use rust_decimal::Decimal;
use talaria_contracts::{PrecisionTier, Quote, RawBar, Trade, TradeAvailability};

use crate::decimal_codec::{quote_order_cmp, trade_order_cmp};

/// Generic ADR-0018 PIT cutoff + maximum-revision selector.
///
/// 1. If `as_of` is `Some(cutoff)`, drops rows where `ts_available > cutoff`.
/// 2. For each distinct identity, selects the row with highest `(revision, ts_available, ts_ingested)`
///    (ties break in favor of later input order).
/// 3. Returns selected rows sorted ascending by `ts_event`.
pub fn latest_visible_revisions<Row, Id, FTs, FAvail, FRev, FId, FIng>(
    rows: impl IntoIterator<Item = Row>,
    as_of: Option<i64>,
    ts_event: FTs,
    ts_available: FAvail,
    revision: FRev,
    identity: FId,
    ts_ingested: FIng,
) -> Vec<Row>
where
    Id: Eq + Hash,
    FTs: Fn(&Row) -> i64,
    FAvail: Fn(&Row) -> i64,
    FRev: Fn(&Row) -> u64,
    FId: Fn(&Row) -> Id,
    FIng: Fn(&Row) -> i64,
{
    type RevisionKey = (u64, i64, i64);
    let mut chosen: HashMap<Id, (RevisionKey, usize, Row)> = HashMap::new();
    for (seq, row) in rows.into_iter().enumerate() {
        let avail = ts_available(&row);
        if let Some(cutoff) = as_of
            && avail > cutoff
        {
            continue;
        }
        let id = identity(&row);
        let version = (revision(&row), avail, ts_ingested(&row));
        let replace = match chosen.get(&id) {
            None => true,
            Some((prev_ver, _, _)) => version >= *prev_ver,
        };
        if replace {
            chosen.insert(id, (version, seq, row));
        }
    }
    let mut out: Vec<(i64, usize, Row)> = chosen
        .into_values()
        .map(|(_, seq, row)| (ts_event(&row), seq, row))
        .collect();
    out.sort_by_key(|(ts, seq, _)| (*ts, *seq));
    out.into_iter().map(|(_, _, row)| row).collect()
}

/// Filter `RawBar` rows by `ts_available <= as_of` and select max `revision` per `ts_event`.
pub fn latest_visible_bars(bars: impl IntoIterator<Item = RawBar>, as_of: Option<i64>) -> Vec<RawBar> {
    latest_visible_revisions(
        bars,
        as_of,
        |b| b.ts_event,
        |b| b.ts_available,
        |b| b.revision,
        |b| b.ts_event,
        |b| b.ts_ingested,
    )
}

fn relative_trade_order_known(first: &Trade, second: &Trade) -> bool {
    if first.availability != second.availability {
        return false;
    }
    if first.availability == TradeAvailability::Inferred {
        return match (
            first.archive_day,
            second.archive_day,
            first.archive_index,
            second.archive_index,
        ) {
            (Some(d1), Some(d2), Some(i1), Some(i2)) => (d1, i1) < (d2, i2),
            _ => false,
        };
    }
    if first.packet_id.is_some() && first.packet_id == second.packet_id {
        return match (first.packet_index, second.packet_index) {
            (Some(p1), Some(p2)) => p1 < p2,
            _ => false,
        };
    }
    match (first.sequence, second.sequence) {
        (Some(s1), Some(s2)) => s1 < s2,
        _ => false,
    }
}

fn relative_quote_order_known(first: &Quote, second: &Quote) -> bool {
    match (
        first.connection_id,
        second.connection_id,
        first.update_id,
        second.update_id,
    ) {
        (Some(c1), Some(c2), Some(u1), Some(u2)) => c1 == c2 && u1 != u2,
        _ => false,
    }
}

fn mark_uncertain_ties<T, FTs, FKnown, FMark>(
    rows: &mut [T],
    ts_event: FTs,
    order_known: FKnown,
    mut set_uncertain: FMark,
) where
    FTs: Fn(&T) -> i64,
    FKnown: Fn(&T, &T) -> bool,
    FMark: FnMut(&mut T),
{
    let mut idx = 0;
    while idx < rows.len() {
        let ts = ts_event(&rows[idx]);
        let mut end = idx + 1;
        while end < rows.len() && ts_event(&rows[end]) == ts {
            end += 1;
        }
        let uncertain = rows[idx..end]
            .windows(2)
            .any(|pair| !order_known(&pair[0], &pair[1]));
        if uncertain {
            for row in &mut rows[idx..end] {
                set_uncertain(row);
            }
        }
        idx = end;
    }
}

/// Select PIT-visible `Trade` revisions, preferring `measured` over `inferred` per `trade_id`
/// and flagging uncertain intra-millisecond ties (`order_uncertain = true`).
pub fn latest_visible_trades(
    rows: Vec<Trade>,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
    as_of: Option<i64>,
) -> Vec<Trade> {
    let measured_ids: HashSet<[u8; 16]> = rows
        .iter()
        .filter(|t| t.availability == TradeAvailability::Measured)
        .map(|t| *t.trade_id.as_bytes())
        .collect();
    let mut filtered: Vec<Trade> = rows
        .into_iter()
        .filter(|t| {
            t.availability == TradeAvailability::Measured || !measured_ids.contains(t.trade_id.as_bytes())
        })
        .collect();
    filtered.sort_by(trade_order_cmp);

    let visible = latest_visible_revisions(
        filtered,
        as_of,
        |t| t.ts_event,
        |t| t.ts_available,
        |t| t.revision,
        |t| *t.trade_id.as_bytes(),
        |t| t.ts_ingested,
    );

    let mut in_range: Vec<Trade> = visible
        .into_iter()
        .filter(|t| {
            start_ts.is_none_or(|lo| t.ts_event >= lo) && end_ts.is_none_or(|hi| t.ts_event <= hi)
        })
        .collect();
    in_range.sort_by(trade_order_cmp);
    mark_uncertain_ties(
        &mut in_range,
        |t| t.ts_event,
        relative_trade_order_known,
        |t| t.order_uncertain = true,
    );
    in_range
}

#[derive(Hash, PartialEq, Eq)]
enum QuoteIdentity {
    ConnUpdate([u8; 16], i64),
    EventTs(i64),
}

/// Select PIT-visible `Quote` revisions and flag uncertain intra-millisecond ties.
pub fn latest_visible_quotes(
    mut rows: Vec<Quote>,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
    as_of: Option<i64>,
) -> Vec<Quote> {
    rows.sort_by(quote_order_cmp);
    let visible = latest_visible_revisions(
        rows,
        as_of,
        |q| q.ts_event,
        |q| q.ts_available,
        |q| q.revision,
        |q| match (q.connection_id, q.update_id) {
            (Some(cid), Some(uid)) => QuoteIdentity::ConnUpdate(*cid.as_bytes(), uid),
            _ => QuoteIdentity::EventTs(q.ts_event),
        },
        |q| q.ts_ingested,
    );
    let mut in_range: Vec<Quote> = visible
        .into_iter()
        .filter(|q| {
            start_ts.is_none_or(|lo| q.ts_event >= lo) && end_ts.is_none_or(|hi| q.ts_event <= hi)
        })
        .collect();
    in_range.sort_by(quote_order_cmp);
    mark_uncertain_ties(
        &mut in_range,
        |q| q.ts_event,
        relative_quote_order_known,
        |q| q.order_uncertain = true,
    );
    in_range
}

// =========================================================================
// Cold/Hot Bar Reconciliation (matches Python `perception/bar_range.py`)
// =========================================================================

#[derive(PartialEq, Eq)]
struct BarValues {
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    turnover: Decimal,
    precision: PrecisionTier,
}

fn bar_values(bar: &RawBar) -> BarValues {
    BarValues {
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        volume: bar.volume,
        turnover: bar.turnover,
        precision: bar.precision,
    }
}

/// Reconcile in-process hot revisions for a single `ts_event` slot against cold revisions.
pub fn reconcile_revisions(
    cold: &[RawBar],
    hot: &[RawBar],
    include_open: bool,
) -> BTreeMap<u64, RawBar> {
    let mut archived: Vec<RawBar> = cold.to_vec();
    archived.sort_by_key(|bar| (bar.revision, bar.ts_available, bar.ts_ingested));

    let mut matches: VecDeque<(u64, usize)> = VecDeque::new();
    let mut cursor = 0usize;
    let mut first_confirmation = true;

    for bar in hot {
        if !bar.confirmed || bar.synthetic {
            continue;
        }
        let pub_key = (bar.ts_ingested, bar_values(bar));
        let mut matched = false;
        for (idx, stored) in archived.iter().enumerate().skip(cursor) {
            if (stored.ts_ingested, bar_values(stored)) == pub_key {
                matches.push_back((bar.revision, idx));
                cursor = idx + 1;
                matched = true;
                break;
            }
        }
        if !matched && first_confirmation {
            for idx in (0..archived.len()).rev() {
                let stored = &archived[idx];
                if stored.ts_ingested > bar.ts_ingested {
                    continue;
                }
                if bar_values(stored) == bar_values(bar) {
                    matches.push_back((bar.revision, idx));
                    cursor = idx + 1;
                    break;
                }
                if stored.ts_ingested < bar.ts_ingested {
                    break;
                }
            }
        }
        first_confirmation = false;
    }

    let mut cursor = matches.front().map(|m| m.1).unwrap_or(archived.len());
    let mut previous: Option<RawBar> = if cursor > 0 {
        Some(archived[cursor - 1].clone())
    } else {
        None
    };
    let mut revision: i128 = previous
        .as_ref()
        .map(|b| i128::from(b.revision))
        .unwrap_or(-1);
    let mut pending = BTreeMap::new();

    for bar_ref in hot {
        let mut bar = bar_ref.clone();
        let hot_revision = bar.revision;
        let (next_revision, next_index) = matches
            .front()
            .map(|&(r, idx)| (Some(r), idx))
            .unwrap_or((None, archived.len()));

        while cursor < next_index && archived[cursor].ts_ingested <= bar.ts_ingested {
            let arch = &archived[cursor];
            revision = revision.max(i128::from(arch.revision));
            previous = Some(arch.clone());
            cursor += 1;
        }
        if Some(bar.revision) == next_revision {
            let arch = &archived[next_index];
            revision = revision.max(i128::from(arch.revision));
            previous = Some(arch.clone());
            cursor = next_index + 1;
            matches.pop_front();
            continue;
        }
        if bar.synthetic || !bar.confirmed {
            if !include_open {
                revision = (i128::from(bar.revision)).max(revision + 1);
                continue;
            }
            bar.confirmed = false;
            bar.ts_available = bar.ts_event.max(bar.ts_ingested);
        } else if bar.history_only {
            if previous.is_some() || !archived.is_empty() {
                continue;
            }
        } else if let Some(prev) = &previous {
            if bar_values(&bar) == bar_values(prev) {
                continue;
            }
            bar.ts_available = bar
                .ts_available
                .max(bar.ts_ingested)
                .max(prev.ts_available);
        }
        revision = i128::from(bar.revision).max(revision + 1);
        bar.revision = revision as u64;
        pending.insert(hot_revision, bar.clone());
        if bar.confirmed {
            previous = Some(bar);
        }
    }
    pending
}

/// Reconcile unpublished hot revisions for a single timestamp slot (`include_open = false`).
pub fn unpublished_revisions(cold: &[RawBar], hot: &[RawBar]) -> BTreeMap<u64, RawBar> {
    reconcile_revisions(cold, hot, false)
}

fn pending_revisions(cold: &[RawBar], hot: &[RawBar], include_open: bool) -> Vec<RawBar> {
    let mut hot_by_event: BTreeMap<i64, Vec<RawBar>> = BTreeMap::new();
    for bar in hot {
        hot_by_event.entry(bar.ts_event).or_default().push(bar.clone());
    }
    let mut cold_by_event: HashMap<i64, Vec<RawBar>> = HashMap::new();
    for bar in cold {
        if hot_by_event.contains_key(&bar.ts_event) {
            cold_by_event
                .entry(bar.ts_event)
                .or_default()
                .push(bar.clone());
        }
    }
    let mut pending = Vec::new();
    for (ts, versions) in hot_by_event {
        let cold_slice = cold_by_event.get(&ts).map(Vec::as_slice).unwrap_or(&[]);
        pending.extend(reconcile_revisions(cold_slice, &versions, include_open).into_values());
    }
    pending
}

/// Shared cold+hot bar selection for HTTP/IPC range queries (`perception/bar_range.py`).
pub fn range_visible_bars(
    cold: &[RawBar],
    hot: &[RawBar],
    as_of: Option<i64>,
    include_open: bool,
) -> Vec<RawBar> {
    let pending = pending_revisions(cold, hot, include_open);
    let closed = latest_visible_bars(
        cold.iter()
            .cloned()
            .chain(pending.iter().filter(|b| b.confirmed).cloned()),
        as_of,
    );
    if !include_open {
        return closed;
    }
    let closed_events: HashSet<i64> = closed.iter().map(|b| b.ts_event).collect();
    let open_bars = latest_visible_bars(
        pending
            .iter()
            .filter(|b| !b.confirmed && !b.synthetic && !closed_events.contains(&b.ts_event))
            .cloned(),
        as_of,
    );
    let mut real_events = closed_events;
    real_events.extend(open_bars.iter().map(|b| b.ts_event));
    let fallback = latest_visible_bars(
        pending
            .iter()
            .filter(|b| b.synthetic && !real_events.contains(&b.ts_event))
            .cloned(),
        as_of,
    );
    let mut combined: Vec<RawBar> = closed
        .into_iter()
        .chain(open_bars)
        .chain(fallback)
        .collect();
    combined.sort_by_key(|b| b.ts_event);
    combined
}

/// Shared confirmed revision window for strategy resync / subscription backfill.
pub fn revision_window(
    cold: &[RawBar],
    hot: &[RawBar],
    since: i64,
    as_of: Option<i64>,
) -> (Vec<RawBar>, Option<i64>) {
    let mut visible = Vec::new();
    let mut next_available: Option<i64> = None;
    let pending = pending_revisions(cold, hot, false);
    for bar in cold.iter().cloned().chain(pending) {
        if !bar.confirmed || bar.synthetic || bar.ts_available < since {
            continue;
        }
        if as_of.is_none_or(|cutoff| bar.ts_available <= cutoff) {
            visible.push(bar);
        } else if next_available.is_none_or(|n| bar.ts_available < n) {
            next_available = Some(bar.ts_available);
        }
    }
    visible.sort_by_key(|b| (b.ts_event, b.revision));
    (visible, next_available)
}
