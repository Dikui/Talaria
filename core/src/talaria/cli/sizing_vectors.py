"""Deterministic cross-language golden vector generator for sizing preview (ADR-0010 / D4).

Python (`RUnitSizer`, `risk_math`, `CryptoPerpMarginValuation`) is the sole mathematical
authority. This module computes preview results directly from those authoritative
implementations and serializes golden vectors to `contracts/sizing_vectors/` so Rust
`talaria-operators::sizing` is gated for character-for-character parity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import json
from pathlib import Path
from typing import Any

from talaria.account.contracts import (
    AccountState,
    DecisionOrigin,
    EntryCandidate,
    RoundExposure,
    RoundStatus,
    SizingRejected,
    TradingState,
)
from talaria.account.plugins.r_unit import RUnitSizer
from talaria.account.plugins.risk_math import sign, unit_risk, valid_entry, worst_entry
from talaria.account.risk_config import AccountRiskConfig
from talaria.foundation.margin import Direction, MarginPosition
from talaria.foundation.market import (
    CarryTrait,
    FeedCapabilityTrait,
    InstrumentSpec,
    MarginTrait,
    OrderConstraintTrait,
    SessionTrait,
    TraitBundle,
)
from talaria.foundation.paper import MarginMode, SizingPreviewResponse
from talaria.foundation.types import decimal_from_finite_fraction, decimal_to_string
from talaria.packs.crypto_perp.margin import CryptoPerpMarginValuation

VECTOR_FILENAMES = (
    "r_unit_sizing.json",
    "manual_override_and_rejections.json",
    "isolated_liquidation.json",
)

_ZERO = Decimal(0)
_DEFAULT_MAINTENANCE_RATE = Decimal("0.005")


def default_vector_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "contracts" / "sizing_vectors"


def _normalize_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return _ZERO
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text) if text and text != "-0" else _ZERO


def _clean_decimal(value: Fraction, *, max_places: int = 8) -> Decimal:
    if value == 0:
        return _ZERO
    denom = value.denominator
    twos = 0
    while denom % 2 == 0:
        denom //= 2
        twos += 1
    fives = 0
    while denom % 5 == 0:
        denom //= 5
        fives += 1
    if denom == 1 and max(twos, fives) <= max_places:
        return _normalize_decimal(decimal_from_finite_fraction(value))
    quant = Decimal(10) ** -max_places
    prec = max(28, len(str(abs(value.numerator))) + max_places + 6)
    with localcontext(prec=prec, rounding=ROUND_HALF_EVEN):
        quotient = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(quant)
    return _normalize_decimal(quotient)


def _fmt_exact(value: Fraction) -> str:
    return decimal_to_string(_normalize_decimal(decimal_from_finite_fraction(value)))


def _fmt_dec(value: Decimal | None) -> str | None:
    return None if value is None else decimal_to_string(_normalize_decimal(value))


@dataclass(frozen=True, slots=True)
class SizingPreviewCaseInput:
    direction: Direction
    entry_low: Decimal
    entry_high: Decimal
    invalidation_stop: Decimal
    qty_step: Decimal
    min_qty: Decimal
    contract_multiplier: Decimal
    equity: Decimal
    r_unit_pct: Decimal = Decimal("0.005")
    max_single_r_pct: Decimal = Decimal("0.01")
    friction_buffer_pct: Decimal = Decimal("0.002")
    margin_mode: MarginMode = MarginMode.ISOLATED
    leverage: Decimal = Decimal("5")
    maintenance_rate: Decimal = _DEFAULT_MAINTENANCE_RATE
    existing_round_risk: Decimal = _ZERO
    existing_round_stop: Decimal | None = None
    override_quantity: Decimal | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "entry_low": decimal_to_string(self.entry_low),
            "entry_high": decimal_to_string(self.entry_high),
            "invalidation_stop": decimal_to_string(self.invalidation_stop),
            "qty_step": decimal_to_string(self.qty_step),
            "min_qty": decimal_to_string(self.min_qty),
            "contract_multiplier": decimal_to_string(self.contract_multiplier),
            "equity": decimal_to_string(self.equity),
            "r_unit_pct": decimal_to_string(self.r_unit_pct),
            "max_single_r_pct": decimal_to_string(self.max_single_r_pct),
            "friction_buffer_pct": decimal_to_string(self.friction_buffer_pct),
            "margin_mode": self.margin_mode.value,
            "leverage": decimal_to_string(self.leverage),
            "maintenance_rate": decimal_to_string(self.maintenance_rate),
            "existing_round_risk": decimal_to_string(self.existing_round_risk),
            "existing_round_stop": (
                None if self.existing_round_stop is None else decimal_to_string(self.existing_round_stop)
            ),
            "override_quantity": (
                None if self.override_quantity is None else decimal_to_string(self.override_quantity)
            ),
        }


class _SyntheticPack:
    """Minimal MarketPack adapter so RUnitSizer computes directly on vector inputs."""

    def __init__(self, spec: InstrumentSpec, margin: MarginTrait) -> None:
        self._spec = spec
        self._traits = TraitBundle(
            feed=FeedCapabilityTrait(realtime=True, polling=True, history=True),
            margin=margin,
            carry=CarryTrait(funding_interval_hours=8),
            session=SessionTrait(session_kind="24x7", timezone="UTC"),
            order_constraint=OrderConstraintTrait(post_only=True),
        )

    def instrument(self, instrument_id: str) -> InstrumentSpec | None:
        return self._spec if instrument_id == self._spec.instrument_id else None

    def traits(self, instrument_id: str) -> TraitBundle:
        if instrument_id != self._spec.instrument_id:
            raise KeyError(instrument_id)
        return self._traits


def compute_authoritative_sizing_preview(case: SizingPreviewCaseInput) -> SizingPreviewResponse:
    """Compute sizing preview using Python's authoritative RUnitSizer & MarginValuation."""
    instrument_id = "SYNTH-PERP"
    sub_account_id = "sa-vector"
    stop = case.existing_round_stop if case.existing_round_stop is not None else case.invalidation_stop

    if case.qty_step <= 0 or case.min_qty <= 0 or case.contract_multiplier <= 0:
        return SizingPreviewResponse(rejected_reason="INVALID_INSTRUMENT_SPEC")
    if case.equity <= 0:
        return SizingPreviewResponse(rejected_reason="NON_POSITIVE_EQUITY")

    candidate = EntryCandidate(
        sub_account_id=sub_account_id,
        instrument_id=instrument_id,
        direction=case.direction,
        entry_low=case.entry_low,
        entry_high=case.entry_high,
        invalidation_stop=case.invalidation_stop,
        take_profit=(case.entry_high + Decimal(1) if case.direction == "LONG" else max(Decimal("0.0001"), case.entry_low - Decimal(1)),),
        ts_decision=1_700_000_000_000,
        origin=DecisionOrigin.HUMAN,
        round_id="rnd-existing" if case.existing_round_stop is not None or case.existing_round_risk > 0 else None,
    )

    if not valid_entry(candidate, stop):
        return SizingPreviewResponse(rejected_reason="INVALID_STOP_GEOMETRY")

    config = AccountRiskConfig(
        initial_capital=case.equity,
        currency="USDT",
        margin_mode=case.margin_mode.value,
        leverage=case.leverage,
        r_unit_pct=case.r_unit_pct,
        max_single_r_pct=case.max_single_r_pct,
        friction_buffer_pct=case.friction_buffer_pct,
    )
    spec = InstrumentSpec(
        instrument_id=instrument_id,
        venue="bybit",
        symbol="SYNTH",
        asset_class="crypto_perp",
        tick_size=Decimal("0.01"),
        qty_step=case.qty_step,
        min_qty=case.min_qty,
        price_precision=2,
        qty_precision=8,
    )
    margin_trait = MarginTrait(
        margin_asset="USDT",
        max_leverage=max(Decimal("100"), case.leverage),
        contract_multiplier=case.contract_multiplier,
    )
    pack = _SyntheticPack(spec, margin_trait)
    valuation = CryptoPerpMarginValuation(
        margin_trait=lambda _iid: margin_trait,
        maintenance_rate=case.maintenance_rate,
    )

    entry = worst_entry(candidate)
    per_unit = unit_risk(case.direction, entry, stop, config) * Fraction(case.contract_multiplier)
    existing_risk = Fraction(case.existing_round_risk)

    def _compute_liquidation(qty: Decimal) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        if case.margin_mode is not MarginMode.ISOLATED:
            return None, None, None
        # Verify exact match with CryptoPerpMarginValuation.isolated_liquidation_price formula
        s = sign(case.direction)
        # Exact rational form of (s * entry - entry / leverage) / (s - maintenance_rate)
        num = Fraction(s) * Fraction(entry) - Fraction(entry) / Fraction(case.leverage)
        den = Fraction(s) - Fraction(case.maintenance_rate)
        liq_frac = max(Fraction(0), num / den)
        # Sanity-check against CryptoPerpMarginValuation directly
        val_liq = valuation.isolated_liquidation_price(
            instrument_id,
            case.direction,
            qty,
            entry,
            entry * qty * case.contract_multiplier / case.leverage,
            as_of=candidate.ts_decision,
        )
        assert abs(val_liq - _clean_decimal(liq_frac, max_places=8)) <= Decimal("0.00000001")
        liq_price = _clean_decimal(liq_frac, max_places=8)
        dist_frac = Fraction(s) * (Fraction(stop) - liq_frac)
        dist = _clean_decimal(dist_frac, max_places=8)
        dist_pct = _clean_decimal(dist_frac / Fraction(entry), max_places=8)
        return liq_price, dist, dist_pct

    if case.override_quantity is not None:
        qty = _normalize_decimal(case.override_quantity)
        if qty <= 0 or qty < case.min_qty:
            return SizingPreviewResponse(rejected_reason="BELOW_MIN_QUANTITY")
        if Fraction(qty) % Fraction(case.qty_step) != 0:
            return SizingPreviewResponse(rejected_reason="INVALID_QUANTITY_STEP")
        risk_amount_frac = per_unit * Fraction(qty)
        total_risk_frac = existing_risk + risk_amount_frac
        risk_pct = _clean_decimal(total_risk_frac / Fraction(case.equity), max_places=8)
        max_allowed = Fraction(case.equity) * Fraction(case.max_single_r_pct)
        basis = (
            f"manual_quantity={_fmt_exact(Fraction(qty))}, worst_entry={_fmt_exact(Fraction(entry))}, "
            f"stop={_fmt_exact(Fraction(stop))}, risk_per_unit={_fmt_exact(per_unit)}"
        )
        liq_price, liq_dist, liq_dist_pct = _compute_liquidation(qty)
        rejected = "EXCEEDS_MAX_SINGLE_R" if total_risk_frac > max_allowed else None
        return SizingPreviewResponse(
            quantity=qty,
            basis=basis,
            risk_amount_r=_normalize_decimal(decimal_from_finite_fraction(risk_amount_frac)),
            risk_pct_of_equity=risk_pct,
            est_liq_price=liq_price,
            est_liq_distance=liq_dist,
            est_liq_distance_pct=liq_dist_pct,
            rejected_reason=rejected,
        )

    # Auto-sizing via RUnitSizer (when no synthetic existing_round_risk) + exact check
    budget = Fraction(case.equity) * Fraction(case.r_unit_pct)
    if candidate.round_id is not None:
        budget = min(budget, Fraction(case.equity) * Fraction(case.max_single_r_pct) - existing_risk)

    if candidate.round_id is None:
        # Cross-verify directly against real RUnitSizer.size()
        state = AccountState(
            sub_account_id=sub_account_id,
            balances={("USDT", "CASH_AVAILABLE"): case.equity},
            rounds={},
            trading_state=TradingState.ACTIVE,
            as_of=candidate.ts_decision,
        )
        sizer = RUnitSizer(pack, {sub_account_id: config})  # type: ignore[arg-type]
        try:
            decision = sizer.size(candidate, state, valuation)
            qty = _normalize_decimal(decision.quantity)
        except SizingRejected as exc:
            return SizingPreviewResponse(rejected_reason=str(exc))
    else:
        if budget <= 0:
            return SizingPreviewResponse(rejected_reason="BELOW_MIN_QUANTITY")
        step = Fraction(case.qty_step)
        lots = budget // (per_unit * step)
        qty = _normalize_decimal(decimal_from_finite_fraction(lots * step))
        if qty < case.min_qty or qty <= 0:
            return SizingPreviewResponse(rejected_reason="BELOW_MIN_QUANTITY")
        # Also verify against RUnitSizer when existing_round_risk == 0 and stop overridden
        if existing_risk == 0:
            rounds = {
                candidate.round_id: RoundStatus(
                    round_id=candidate.round_id,
                    sub_account_id=sub_account_id,
                    instrument_id=instrument_id,
                    direction=case.direction,
                    quantity=_ZERO,
                    stop=stop,
                )
            }
            state = AccountState(
                sub_account_id=sub_account_id,
                balances={("USDT", "CASH_AVAILABLE"): case.equity},
                rounds=rounds,
                trading_state=TradingState.ACTIVE,
                as_of=candidate.ts_decision,
                positions=(
                    RoundExposure(
                        candidate.round_id,
                        MarginPosition(instrument_id, case.direction, _ZERO, entry),
                        stop,
                    ),
                ),
            )
            sizer = RUnitSizer(pack, {sub_account_id: config})  # type: ignore[arg-type]
            assert _normalize_decimal(sizer.size(candidate, state, valuation).quantity) == qty

    risk_amount_frac = per_unit * Fraction(qty)
    total_risk_frac = existing_risk + risk_amount_frac
    risk_pct = _clean_decimal(total_risk_frac / Fraction(case.equity), max_places=8)
    basis = (
        f"risk_budget={_fmt_exact(budget)}, worst_entry={_fmt_exact(Fraction(entry))}, "
        f"stop={_fmt_exact(Fraction(stop))}, risk_per_unit={_fmt_exact(per_unit)}"
    )
    liq_price, liq_dist, liq_dist_pct = _compute_liquidation(qty)
    return SizingPreviewResponse(
        quantity=qty,
        basis=basis,
        risk_amount_r=_normalize_decimal(decimal_from_finite_fraction(risk_amount_frac)),
        risk_pct_of_equity=risk_pct,
        est_liq_price=liq_price,
        est_liq_distance=liq_dist,
        est_liq_distance_pct=liq_dist_pct,
        rejected_reason=None,
    )


def _serialize_expected(resp: SizingPreviewResponse) -> dict[str, Any]:
    return {
        "quantity": _fmt_dec(resp.quantity),
        "basis": resp.basis,
        "risk_amount_r": _fmt_dec(resp.risk_amount_r),
        "risk_pct_of_equity": _fmt_dec(resp.risk_pct_of_equity),
        "est_liq_price": _fmt_dec(resp.est_liq_price),
        "est_liq_distance": _fmt_dec(resp.est_liq_distance),
        "est_liq_distance_pct": _fmt_dec(resp.est_liq_distance_pct),
        "rejected_reason": resp.rejected_reason,
    }


def _build_cases() -> dict[str, list[tuple[str, SizingPreviewCaseInput]]]:
    return {
        "r_unit_sizing.json": [
            (
                "long_btc_default_c_buf",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                ),
            ),
            (
                "short_eth_default_c_buf",
                SizingPreviewCaseInput(
                    direction="SHORT",
                    entry_low=Decimal("3200"),
                    entry_high=Decimal("3220"),
                    invalidation_stop=Decimal("3280"),
                    qty_step=Decimal("0.01"),
                    min_qty=Decimal("0.01"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("25000"),
                    r_unit_pct=Decimal("0.0075"),
                    max_single_r_pct=Decimal("0.015"),
                ),
            ),
            (
                "contract_multiplier_10_step_rounding",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("100"),
                    entry_high=Decimal("101"),
                    invalidation_stop=Decimal("98"),
                    qty_step=Decimal("0.1"),
                    min_qty=Decimal("0.2"),
                    contract_multiplier=Decimal("10"),
                    equity=Decimal("50000"),
                    friction_buffer_pct=Decimal("0.0025"),
                ),
            ),
            (
                "contract_multiplier_0_01_integer_lots",
                SizingPreviewCaseInput(
                    direction="SHORT",
                    entry_low=Decimal("50000"),
                    entry_high=Decimal("50100"),
                    invalidation_stop=Decimal("50500"),
                    qty_step=Decimal("1"),
                    min_qty=Decimal("1"),
                    contract_multiplier=Decimal("0.01"),
                    equity=Decimal("12000"),
                    friction_buffer_pct=Decimal("0.001"),
                ),
            ),
            (
                "zero_friction_exact_boundary_floor",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("100"),
                    entry_high=Decimal("100"),
                    invalidation_stop=Decimal("90"),
                    qty_step=Decimal("0.5"),
                    min_qty=Decimal("0.5"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    r_unit_pct=Decimal("0.005"),
                    friction_buffer_pct=Decimal("0"),
                ),
            ),
            (
                "scaling_addon_capped_by_remaining_max_single_r",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64800"),
                    entry_high=Decimal("64900"),
                    invalidation_stop=Decimal("64200"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    r_unit_pct=Decimal("0.005"),
                    max_single_r_pct=Decimal("0.01"),
                    existing_round_risk=Decimal("70"),
                    existing_round_stop=Decimal("64000"),
                ),
            ),
        ],
        "manual_override_and_rejections.json": [
            (
                "manual_override_within_max_single_r",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    override_quantity=Decimal("0.100"),
                ),
            ),
            (
                "manual_override_exceeds_max_single_r",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    override_quantity=Decimal("0.200"),
                ),
            ),
            (
                "manual_override_invalid_step",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.01"),
                    min_qty=Decimal("0.01"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    override_quantity=Decimal("0.015"),
                ),
            ),
            (
                "manual_override_below_min_qty",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.01"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    override_quantity=Decimal("0.005"),
                ),
            ),
            (
                "reject_invalid_stop_geometry_long",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64500"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                ),
            ),
            (
                "reject_invalid_stop_geometry_short",
                SizingPreviewCaseInput(
                    direction="SHORT",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64550"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                ),
            ),
            (
                "reject_inverted_entry_band",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64700"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                ),
            ),
            (
                "reject_below_min_quantity_rounds_to_zero",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("60000"),
                    qty_step=Decimal("0.01"),
                    min_qty=Decimal("0.01"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("500"),
                ),
            ),
            (
                "reject_scaling_round_budget_exhausted",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64800"),
                    entry_high=Decimal("64900"),
                    invalidation_stop=Decimal("64200"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    existing_round_risk=Decimal("105"),
                    existing_round_stop=Decimal("64000"),
                ),
            ),
        ],
        "isolated_liquidation.json": [
            (
                "isolated_long_5x_safe_stop",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    margin_mode=MarginMode.ISOLATED,
                    leverage=Decimal("5"),
                ),
            ),
            (
                "isolated_short_10x_safe_stop",
                SizingPreviewCaseInput(
                    direction="SHORT",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("65500"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("20000"),
                    margin_mode=MarginMode.ISOLATED,
                    leverage=Decimal("10"),
                ),
            ),
            (
                "isolated_long_50x_stop_beyond_liquidation_negative_distance",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("62000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("100000"),
                    margin_mode=MarginMode.ISOLATED,
                    leverage=Decimal("50"),
                ),
            ),
            (
                "cross_margin_omits_isolated_liquidation",
                SizingPreviewCaseInput(
                    direction="LONG",
                    entry_low=Decimal("64500"),
                    entry_high=Decimal("64600"),
                    invalidation_stop=Decimal("64000"),
                    qty_step=Decimal("0.001"),
                    min_qty=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    equity=Decimal("10000"),
                    margin_mode=MarginMode.CROSS,
                    leverage=Decimal("5"),
                ),
            ),
        ],
    }


def render_sizing_vectors() -> dict[str, str]:
    rendered: dict[str, str] = {}
    for filename, entries in _build_cases().items():
        payload = {
            "version": 1,
            "suite": filename.removesuffix(".json"),
            "cases": [
                {
                    "name": name,
                    "input": case.to_json_dict(),
                    "expected": _serialize_expected(compute_authoritative_sizing_preview(case)),
                }
                for name, case in entries
            ],
        }
        rendered[filename] = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return rendered
