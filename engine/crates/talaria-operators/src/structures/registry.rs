//! Pluggable `StructureDetector` and `PostProcessor` traits + `DetectorRegistry`.

use std::collections::{BTreeMap, BTreeSet};

use talaria_contracts::structures::{STRUCTURE_METHOD_VERSION, StructureFamily, StructureFeature, StructureSet};

use crate::error::OperatorError;
use crate::structures::context::DetectionContext;

pub trait StructureDetector: Send + Sync {
    fn detector_id(&self) -> &'static str;
    fn version(&self) -> &'static str;
    fn families(&self) -> &'static [StructureFamily];
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError>;
}

pub trait PostProcessor: Send + Sync {
    fn processor_id(&self) -> &'static str;
    fn process(
        &self,
        ctx: &DetectionContext,
        features: Vec<StructureFeature>,
    ) -> Result<Vec<StructureFeature>, OperatorError>;
}

#[derive(Default)]
pub struct DetectorRegistry {
    detectors: BTreeMap<String, Box<dyn StructureDetector>>,
    detector_order: Vec<String>,
    postprocessors: BTreeMap<String, Box<dyn PostProcessor>>,
    postprocessor_order: Vec<String>,
}

impl DetectorRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register_detector(&mut self, detector: Box<dyn StructureDetector>) -> Result<(), OperatorError> {
        let did = detector.detector_id().to_owned();
        if self.detectors.contains_key(&did) {
            return Err(OperatorError::InvalidStructureParam(format!(
                "duplicate structure detector: {did}"
            )));
        }
        self.detectors.insert(did.clone(), detector);
        self.detector_order.push(did);
        Ok(())
    }

    pub fn register_postprocessor(
        &mut self,
        processor: Box<dyn PostProcessor>,
    ) -> Result<(), OperatorError> {
        let pid = processor.processor_id().to_owned();
        if self.postprocessors.contains_key(&pid) {
            return Err(OperatorError::InvalidStructureParam(format!(
                "duplicate structure postprocessor: {pid}"
            )));
        }
        self.postprocessors.insert(pid.clone(), processor);
        self.postprocessor_order.push(pid);
        Ok(())
    }

    pub fn list_detectors(&self) -> &[String] {
        &self.detector_order
    }

    pub fn compute_structures(
        &self,
        ctx: &DetectionContext,
        snapshot_id: &str,
        as_of_ts: Option<i64>,
        detector_ids: Option<&[String]>,
        enable_postprocessors: bool,
        notes: &[String],
    ) -> Result<StructureSet, OperatorError> {
        let params_map = ctx.params.as_parameters()?;
        if ctx.visible.is_empty() || ctx.analysis_window.is_none() {
            return Ok(StructureSet {
                snapshot_id: snapshot_id.to_owned(),
                instrument_id: ctx.instrument_id.clone(),
                timeframe: ctx.timeframe.clone(),
                cutoff_ts: ctx.cutoff_ts,
                as_of_ts: as_of_ts.unwrap_or(ctx.cutoff_ts),
                method_version: STRUCTURE_METHOD_VERSION.to_owned(),
                detector_ids: Vec::new(),
                parameters: params_map,
                notes: notes.to_vec(),
                structures: Vec::new(),
            });
        }

        let selected: Vec<String> = match detector_ids {
            Some(ids) => ids.to_vec(),
            None => self.detector_order.clone(),
        };
        for did in &selected {
            if !self.detectors.contains_key(did) {
                return Err(OperatorError::InvalidStructureParam(format!(
                    "unregistered structure detector: {did}"
                )));
            }
        }

        let mut seen_ids = BTreeSet::new();
        let mut features = Vec::new();
        for did in &selected {
            let detector = &self.detectors[did];
            for item in detector.detect(ctx)? {
                if item.ts_available <= ctx.cutoff_ts && seen_ids.insert(item.structure_id.clone()) {
                    features.push(item);
                }
            }
        }

        if enable_postprocessors {
            for pid in &self.postprocessor_order {
                let proc = &self.postprocessors[pid];
                features = proc.process(ctx, features)?;
            }
        }

        features.sort_by(|a, b| {
            b.strength
                .total_cmp(&a.strength)
                .then_with(|| b.ts_available.cmp(&a.ts_available))
                .then_with(|| a.structure_id.cmp(&b.structure_id))
        });

        Ok(StructureSet {
            snapshot_id: snapshot_id.to_owned(),
            instrument_id: ctx.instrument_id.clone(),
            timeframe: ctx.timeframe.clone(),
            cutoff_ts: ctx.cutoff_ts,
            as_of_ts: as_of_ts.unwrap_or(ctx.cutoff_ts),
            method_version: STRUCTURE_METHOD_VERSION.to_owned(),
            detector_ids: selected,
            parameters: params_map,
            notes: notes.to_vec(),
            structures: features,
        })
    }
}
