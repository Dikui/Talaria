//! Unified Market Structure Engine in pure Rust (`talaria-operators`).

pub mod context;
pub mod detectors;
pub mod geometry;
pub mod postprocess;
pub mod registry;

use talaria_contracts::bars::CompactBar;
use talaria_contracts::derivatives::DerivativePoint;
use talaria_contracts::key_levels::SessionProfile;
use talaria_contracts::structures::StructureSet;

use crate::error::OperatorError;
pub use context::{ConfirmedPivot, DetectionContext, PivotSide, StructureParams, make_structure_id};
use detectors::{
    ConvexEnvelopeDetector, EqualLevelsDetector, FvgDetector, GapLevelsDetector,
    LiquidationZonesDetector, PivotTrendlineDetector, RoundNumbersDetector, SessionLevelsDetector,
    SwingLevelsDetector, VolumeProfileDetector,
};
pub use geometry::{
    GeometryBounds, geometry_bounds_at, geometry_distance, geometry_to_drawing,
    interpolate_slope_price,
};
use postprocess::{
    BudgetFilterPostProcessor, ConfluencePostProcessor, DeduplicatorPostProcessor,
    LifecyclePostProcessor,
};
pub use registry::{DetectorRegistry, PostProcessor, StructureDetector};

pub fn default_structure_registry() -> DetectorRegistry {
    let mut reg = DetectorRegistry::new();
    let _ = reg.register_detector(Box::new(SwingLevelsDetector));
    let _ = reg.register_detector(Box::new(SessionLevelsDetector));
    let _ = reg.register_detector(Box::new(GapLevelsDetector));
    let _ = reg.register_detector(Box::new(RoundNumbersDetector));
    let _ = reg.register_detector(Box::new(VolumeProfileDetector));
    let _ = reg.register_detector(Box::new(LiquidationZonesDetector));
    let _ = reg.register_detector(Box::new(FvgDetector));
    let _ = reg.register_detector(Box::new(EqualLevelsDetector));
    let _ = reg.register_detector(Box::new(PivotTrendlineDetector));
    let _ = reg.register_detector(Box::new(ConvexEnvelopeDetector));

    let _ = reg.register_postprocessor(Box::new(LifecyclePostProcessor));
    let _ = reg.register_postprocessor(Box::new(DeduplicatorPostProcessor));
    let _ = reg.register_postprocessor(Box::new(ConfluencePostProcessor));
    let _ = reg.register_postprocessor(Box::new(BudgetFilterPostProcessor));
    reg
}

#[allow(clippy::too_many_arguments)]
pub fn compute_structure_set(
    snapshot_id: &str,
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    as_of_ts: Option<i64>,
    session_profile: Option<&SessionProfile>,
    params: Option<&StructureParams>,
    derivative_points: &[DerivativePoint],
    detector_ids: Option<&[String]>,
    registry: Option<&DetectorRegistry>,
) -> Result<StructureSet, OperatorError> {
    let ctx = DetectionContext::build(
        instrument_id,
        timeframe,
        bars,
        cutoff_ts,
        session_profile,
        params,
        derivative_points,
    )?;
    if let Some(reg) = registry {
        reg.compute_structures(&ctx, snapshot_id, as_of_ts, detector_ids, true, &[])
    } else {
        let default_reg = default_structure_registry();
        default_reg.compute_structures(&ctx, snapshot_id, as_of_ts, detector_ids, true, &[])
    }
}
