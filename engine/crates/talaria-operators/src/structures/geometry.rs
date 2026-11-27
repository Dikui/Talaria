//! Pure-Rust geometry bounds evaluation and adoption mapping for `StructureGeometry`.

use core::cmp::Ordering;
use rust_decimal::Decimal;
use talaria_contracts::drawings::{DrawingKind, DrawingPoint};
use talaria_contracts::structures::{
    PriceScaleMode, SlopeLineGeometry, StructureAnchor, StructureGeometry,
};

use crate::error::OperatorError;
use crate::gda::GdaDecimal;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GeometryBounds {
    pub lower: Decimal,
    pub upper: Decimal,
}

pub fn quantize_price8(value: &GdaDecimal) -> Result<Decimal, OperatorError> {
    value.quantize_exp(-8)?.to_decimal()
}

pub fn interpolate_slope_price(
    anchor_start: &StructureAnchor,
    anchor_end: &StructureAnchor,
    ts: i64,
    scale: PriceScaleMode,
) -> Result<Decimal, OperatorError> {
    let t1 = anchor_start.ts;
    let t2 = anchor_end.ts;
    let p1 = anchor_start.price;
    let p2 = anchor_end.price;
    if t2 == t1 || ts == t1 {
        return Ok(p1);
    }
    if ts == t2 || p1 == p2 {
        return Ok(p2);
    }
        if scale == PriceScaleMode::Log {
        if p1 <= Decimal::ZERO || p2 <= Decimal::ZERO {
            return Err(OperatorError::InvalidStructureParam(
                "LOG scale interpolation requires strictly positive anchor prices".to_owned(),
            ));
        }
        let p1_f = p1.to_string().parse::<f64>().unwrap_or(0.0);
        let p2_f = p2.to_string().parse::<f64>().unwrap_or(0.0);
        let ratio = (ts - t1) as f64 / (t2 - t1) as f64;
        let val = p1_f * ((p2_f / p1_f).ln() * ratio).exp();
        let rounded = format!("{val:.8}");
        return Decimal::from_str_exact(&rounded).or_else(|_| {
            rounded
                .parse::<Decimal>()
                .map_err(|_| OperatorError::InvalidStructureParam("invalid LOG price".to_owned()))
        });
    }

    let g_p1 = GdaDecimal::from_decimal(p1);
    let g_p2 = GdaDecimal::from_decimal(p2);
    let g_num = GdaDecimal::from_i64(ts - t1);
    let g_den = GdaDecimal::from_i64(t2 - t1);
    let ratio = g_num.div(&g_den)?;
    let interp = g_p1.add(&g_p2.sub(&g_p1).mul(&ratio));
    quantize_price8(&interp)
}

fn slope_bounds(
    anchor_start: &StructureAnchor,
    anchor_end: &StructureAnchor,
    scale: PriceScaleMode,
    extend_right: bool,
    ts: i64,
) -> Result<Option<GeometryBounds>, OperatorError> {
    if ts < anchor_start.ts {
        return Ok(None);
    }
    if !extend_right && ts > anchor_end.ts {
        return Ok(None);
    }
    let price = interpolate_slope_price(anchor_start, anchor_end, ts, scale)?;
    Ok(Some(GeometryBounds {
        lower: price,
        upper: price,
    }))
}

fn slope_struct_bounds(
    line: &SlopeLineGeometry,
    ts: i64,
) -> Result<Option<GeometryBounds>, OperatorError> {
    slope_bounds(
        &line.anchor_start,
        &line.anchor_end,
        line.scale,
        line.extend_right,
        ts,
    )
}

pub fn geometry_bounds_at(
    geometry: &StructureGeometry,
    ts: i64,
) -> Result<Option<GeometryBounds>, OperatorError> {
    match geometry {
        StructureGeometry::HorizontalLine { price } => Ok(Some(GeometryBounds {
            lower: *price,
            upper: *price,
        })),
        StructureGeometry::HorizontalZone {
            price_low,
            price_high,
            start_ts,
            end_ts,
        } => {
            if let Some(s) = start_ts
                && ts < *s
            {
                return Ok(None);
            }
            if let Some(e) = end_ts
                && ts > *e
            {
                return Ok(None);
            }
            Ok(Some(GeometryBounds {
                lower: *price_low,
                upper: *price_high,
            }))
        }
        StructureGeometry::SlopeLine {
            anchor_start,
            anchor_end,
            scale,
            extend_right,
        } => slope_bounds(anchor_start, anchor_end, *scale, *extend_right, ts),
        StructureGeometry::Channel { upper, lower } => {
            let Some(u) = slope_struct_bounds(upper, ts)? else {
                return Ok(None);
            };
            let Some(l) = slope_struct_bounds(lower, ts)? else {
                return Ok(None);
            };
            Ok(Some(GeometryBounds {
                lower: l.lower.min(u.lower),
                upper: l.upper.max(u.upper),
            }))
        }
    }
}

pub fn geometry_distance(price: Decimal, bounds: &GeometryBounds) -> Decimal {
    match (price.cmp(&bounds.lower), price.cmp(&bounds.upper)) {
        (Ordering::Less, _) => bounds.lower - price,
        (_, Ordering::Greater) => price - bounds.upper,
        _ => Decimal::ZERO,
    }
}

pub fn geometry_to_drawing(
    geometry: &StructureGeometry,
    fallback_ts: i64,
    bar_ms: i64,
) -> (DrawingKind, Vec<DrawingPoint>) {
    let span_ms = bar_ms.max(1) * 10;
    match geometry {
        StructureGeometry::HorizontalLine { price } => (
            DrawingKind::HorizontalLine,
            vec![DrawingPoint {
                ts: fallback_ts,
                price: *price,
            }],
        ),
        StructureGeometry::HorizontalZone {
            price_low,
            price_high,
            start_ts,
            end_ts,
        } => {
            let start = start_ts.unwrap_or(fallback_ts);
            let mut end = end_ts.unwrap_or(start + span_ms);
            if end <= start {
                end = start + span_ms;
            }
            (
                DrawingKind::Rectangle,
                vec![
                    DrawingPoint {
                        ts: start,
                        price: *price_high,
                    },
                    DrawingPoint {
                        ts: end,
                        price: *price_low,
                    },
                ],
            )
        }
        StructureGeometry::SlopeLine {
            anchor_start,
            anchor_end,
            extend_right,
            ..
        } => {
            let kind = if *extend_right {
                DrawingKind::Ray
            } else {
                DrawingKind::Segment
            };
            (
                kind,
                vec![
                    DrawingPoint {
                        ts: anchor_start.ts,
                        price: anchor_start.price,
                    },
                    DrawingPoint {
                        ts: anchor_end.ts,
                        price: anchor_end.price,
                    },
                ],
            )
        }
        StructureGeometry::Channel { upper, lower } => (
            DrawingKind::ParallelChannel,
            vec![
                DrawingPoint {
                    ts: lower.anchor_start.ts,
                    price: lower.anchor_start.price,
                },
                DrawingPoint {
                    ts: lower.anchor_end.ts,
                    price: lower.anchor_end.price,
                },
                DrawingPoint {
                    ts: upper.anchor_start.ts,
                    price: upper.anchor_start.price,
                },
            ],
        ),
    }
}
