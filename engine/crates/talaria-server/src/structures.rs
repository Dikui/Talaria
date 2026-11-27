//! In-process lookahead-free Market Structure Registry (`StructureRegistry`) with MTFA support.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use talaria_contracts::bars::CompactBar;
use talaria_contracts::derivatives::DerivativePoint;
use talaria_contracts::key_levels::SessionProfile;
use talaria_contracts::structures::{StructureFeature, StructureSet};
use talaria_operators::{OperatorError, StructureParams, compute_structure_set};

pub const MAX_VERSIONS_PER_SERIES: usize = 256;

type SeriesKey = (String, String);
type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;
type OnLatestFn = Arc<dyn Fn(&StructureSet) + Send + Sync>;

pub struct StructureRegistry {
    now_ms: NowFn,
    versions: Mutex<HashMap<SeriesKey, Vec<StructureSet>>>,
    on_latest: Mutex<Option<OnLatestFn>>,
}

impl StructureRegistry {
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

    pub fn register(&self, version: StructureSet) {
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
                .map(|last| {
                    last.cutoff_ts == version.cutoff_ts && last.snapshot_id == version.snapshot_id
                })
                .unwrap_or(false)
        };
        if is_latest
            && let Some(cb) = self.on_latest.lock().expect("on_latest poisoned").clone()
        {
            cb(&version);
        }
    }

    pub fn latest(&self, instrument_id: &str, timeframe: &str) -> Option<StructureSet> {
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
    ) -> Option<StructureSet> {
        let guard = self.versions.lock().expect("versions lock poisoned");
        let list = guard.get(&(instrument_id.to_owned(), timeframe.to_owned()))?;
        let end = list.partition_point(|v| v.cutoff_ts <= cutoff_ts);
        if end == 0 {
            None
        } else {
            Some(list[end - 1].clone())
        }
    }

    pub fn structures_at(
        &self,
        instrument_id: &str,
        timeframe: &str,
        cutoff_ts: i64,
    ) -> Vec<StructureFeature> {
        match self.version_at(instrument_id, timeframe, cutoff_ts) {
            Some(version) => version
                .structures
                .into_iter()
                .filter(|st| st.ts_available <= cutoff_ts)
                .collect(),
            None => Vec::new(),
        }
    }

    /// Collect base timeframe structures plus higher-timeframe MTFA projections at the same `cutoff_ts`.
    pub fn structures_with_mtfa(
        &self,
        instrument_id: &str,
        timeframe: &str,
        htf_timeframes: &[String],
        cutoff_ts: i64,
    ) -> Vec<StructureFeature> {
        let mut out = self.structures_at(instrument_id, timeframe, cutoff_ts);
        for htf in htf_timeframes {
            if htf == timeframe {
                continue;
            }
            for mut feat in self.structures_at(instrument_id, htf, cutoff_ts) {
                feat.source_timeframe = Some(htf.clone());
                out.push(feat);
            }
        }
        out
    }

    pub fn lookup_structure_map(
        &self,
        instrument_id: &str,
        timeframe: &str,
        cutoff_ts: i64,
    ) -> HashMap<String, StructureFeature> {
        self.structures_at(instrument_id, timeframe, cutoff_ts)
            .into_iter()
            .map(|st| (st.structure_id.clone(), st))
            .collect()
    }

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
        params: Option<&StructureParams>,
        derivative_points: &[DerivativePoint],
    ) -> Result<StructureSet, OperatorError> {
        let struct_set = compute_structure_set(
            &snapshot_id,
            instrument_id,
            timeframe,
            bars,
            cutoff_ts,
            Some(as_of_ts),
            session_profile,
            params,
            derivative_points,
            None,
            None,
        )?;
        self.register(struct_set.clone());
        Ok(struct_set)
    }
}
