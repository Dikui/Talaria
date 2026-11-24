//! In-process lookahead-free Key Level Registry (`KeyLevelRegistry`).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use talaria_contracts::bars::CompactBar;
use talaria_contracts::derivatives::DerivativePoint;
use talaria_contracts::key_levels::{KeyLevel, KeyLevelSet, SessionProfile};
use talaria_operators::{KeyLevelParams, compute_key_level_set};

pub const MAX_VERSIONS_PER_SERIES: usize = 256;

type SeriesKey = (String, String);
type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;
type OnLatestFn = Arc<dyn Fn(&KeyLevelSet) + Send + Sync>;

/// Versioned registry of deterministic, lookahead-free key levels per `(instrument, timeframe)`.
pub struct KeyLevelRegistry {
    now_ms: NowFn,
    versions: Mutex<HashMap<SeriesKey, Vec<KeyLevelSet>>>,
    on_latest: Mutex<Option<OnLatestFn>>,
}

impl KeyLevelRegistry {
    pub fn new(now_ms: NowFn) -> Self {
        Self {
            now_ms,
            versions: Mutex::new(HashMap::new()),
            on_latest: Mutex::new(None),
        }
    }

    pub fn set_on_latest(&self, callback: OnLatestFn) {
        *self.on_latest.lock().expect("on_latest lock poisoned") = Some(callback);
    }

    pub fn now_ms(&self) -> i64 {
        (self.now_ms)()
    }

    pub fn register(&self, version: KeyLevelSet) {
        let published = (self.now_ms)();
        if version.cutoff_ts > published {
            return;
        }
        let is_latest = {
            let mut guard = self.versions.lock().expect("versions lock poisoned");
            let key = (version.instrument_id.clone(), version.timeframe.clone());
            let list = guard.entry(key).or_default();
            let pos = list.partition_point(|v| v.cutoff_ts <= version.cutoff_ts);
            if pos > 0 && list[pos - 1].cutoff_ts == version.cutoff_ts {
                list[pos - 1] = version.clone();
            } else {
                list.insert(pos, version.clone());
                if list.len() > MAX_VERSIONS_PER_SERIES {
                    list.remove(0);
                }
            }
            list.last()
                .map(|last| last.cutoff_ts == version.cutoff_ts && last.snapshot_id == version.snapshot_id)
                .unwrap_or(false)
        };
        if is_latest
            && let Some(cb) = self.on_latest.lock().expect("on_latest poisoned").clone()
        {
            cb(&version);
        }
    }

    pub fn latest(&self, instrument_id: &str, timeframe: &str) -> Option<KeyLevelSet> {
        let guard = self.versions.lock().expect("versions lock poisoned");
        guard
            .get(&(instrument_id.to_owned(), timeframe.to_owned()))
            .and_then(|v| v.last().cloned())
    }

    pub fn version_at(
        &self,
        instrument_id: &str,
        timeframe: &str,
        cutoff_ts: i64,
    ) -> Option<KeyLevelSet> {
        let guard = self.versions.lock().expect("versions lock poisoned");
        let list = guard.get(&(instrument_id.to_owned(), timeframe.to_owned()))?;
        let end = list.partition_point(|v| v.cutoff_ts <= cutoff_ts);
        if end == 0 {
            None
        } else {
            Some(list[end - 1].clone())
        }
    }

    pub fn levels_at(&self, instrument_id: &str, timeframe: &str, cutoff_ts: i64) -> Vec<KeyLevel> {
        match self.version_at(instrument_id, timeframe, cutoff_ts) {
            Some(version) => version
                .levels
                .into_iter()
                .filter(|lv| lv.ts_available <= cutoff_ts)
                .collect(),
            None => Vec::new(),
        }
    }

    pub fn current_levels(&self, instrument_id: &str, timeframe: &str) -> Vec<KeyLevel> {
        self.levels_at(instrument_id, timeframe, (self.now_ms)())
    }

    /// Compute a `KeyLevelSet` via `talaria-operators`, register it, and return it.
    #[allow(clippy::too_many_arguments)]
    pub fn compute_and_register(
        &self,
        snapshot_id: String,
        instrument_id: &str,
        timeframe: &str,
        bars: &[CompactBar],
        cutoff_ts: i64,
        as_of_ts: i64,
        session_profile: Option<&SessionProfile>,
        params: Option<&KeyLevelParams>,
        derivative_points: &[DerivativePoint],
    ) -> Result<KeyLevelSet, talaria_operators::OperatorError> {
        let mut level_set = compute_key_level_set(
            &snapshot_id,
            instrument_id,
            timeframe,
            bars,
            cutoff_ts,
            session_profile,
            params,
            derivative_points,
        )?;
        level_set.as_of_ts = as_of_ts;
        self.register(level_set.clone());
        Ok(level_set)
    }

    pub fn lookup_level_map(&self, instrument_id: &str, timeframe: &str, cutoff_ts: i64) -> HashMap<String, KeyLevel> {
        self.levels_at(instrument_id, timeframe, cutoff_ts)
            .into_iter()
            .map(|lv| (lv.level_id.clone(), lv))
            .collect()
    }
}
