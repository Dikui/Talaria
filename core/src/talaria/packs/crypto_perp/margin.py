"""线性永续的研究估值模型，使用固定维持保证金率。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import ClassVar

from talaria.foundation.margin import MARGIN_VALUATION_VERSION, Direction, MarginEstimate, MarginPortfolio
from talaria.foundation.market import MarginTrait
from talaria.foundation.types import decimal_from_finite_fraction


@dataclass(frozen=True, slots=True)
class CryptoPerpMarginValuation:
    interface_version: ClassVar[int] = MARGIN_VALUATION_VERSION
    margin_trait: Callable[[str], MarginTrait]
    maintenance_rate: Decimal = Decimal("0.005")

    def isolated_liquidation_price(
        self, instrument_id: str, direction: Direction, quantity: Decimal,
        entry_price: Decimal, isolated_margin: Decimal, *, as_of: int,
    ) -> Decimal:
        units = quantity * self.margin_trait(instrument_id).contract_multiplier
        if units <= 0 or entry_price <= 0 or isolated_margin < 0:
            raise ValueError("逐仓估值需要正数量、正入场价和非负保证金")
        sign = 1 if direction == "LONG" else -1
        # isolated_margin + sign * units * (mark - entry) = units * mark * MMR
        return max(Decimal(0), (sign * entry_price - isolated_margin / units) / (
            sign - self.maintenance_rate
        ))

    def cross_margin(
        self, portfolio: MarginPortfolio, mark_prices: Mapping[str, Decimal], *, as_of: int,
    ) -> MarginEstimate:
        equity = Fraction(portfolio.collateral)
        initial_notional = maintenance_notional = Fraction(0)
        for position, margin_entry in (
            *((item, None) for item in portfolio.positions),
            *((item, item.max_entry_price) for item in portfolio.pending_entries),
        ):
            spec = self.margin_trait(position.instrument_id)
            if not 1 <= portfolio.leverage <= spec.max_leverage:
                raise ValueError(f"{position.instrument_id} 杠杆超出市场约束")
            mark = mark_prices.get(position.instrument_id)
            if mark is None or not mark.is_finite() or mark <= 0:
                raise ValueError(f"缺少有效标记价：{position.instrument_id}")
            units = Fraction(position.quantity) * Fraction(spec.contract_multiplier)
            entry, mark = Fraction(position.entry_price), Fraction(mark)
            pnl = (mark - entry) * units * (1 if position.direction == "LONG" else -1)
            equity += pnl if margin_entry is None else min(0, pnl)
            initial_notional += (mark if margin_entry is None else max(mark, Fraction(margin_entry))) * units
            maintenance_notional += mark * units
        return MarginEstimate(
            equity=decimal_from_finite_fraction(equity),
            initial_margin=decimal_from_finite_fraction(initial_notional) / portfolio.leverage,
            maintenance_margin=decimal_from_finite_fraction(maintenance_notional * Fraction(self.maintenance_rate)),
        )

    def cross_liquidation_price(
        self, portfolio: MarginPortfolio, mark_prices: Mapping[str, Decimal],
        instrument_id: str, *, as_of: int,
    ) -> Decimal | None:
        estimate = self.cross_margin(portfolio, mark_prices, as_of=as_of)
        positions = tuple(p for p in portfolio.positions if p.instrument_id == instrument_id)
        pending = tuple(p for p in portfolio.pending_entries if p.instrument_id == instrument_id)
        if not positions and not pending:
            return None

        # 每笔待成交单在入场价处从计亏损变为零浮盈，逐段求净值等于维持保证金的价格。
        mark = Fraction(mark_prices[instrument_id])
        multiplier = Fraction(self.margin_trait(instrument_id).contract_multiplier)
        all_positions = (*positions, *pending)
        slope = -sum(Fraction(p.quantity) for p in all_positions) * multiplier * Fraction(self.maintenance_rate)
        net_units = sum(Fraction(p.quantity) * (1 if p.direction == "LONG" else -1) for p in all_positions) * multiplier
        direction = 1 if net_units + slope > 0 else -1
        intercept = Fraction(estimate.equity) - Fraction(estimate.maintenance_margin) - slope * mark
        for p in positions:
            units = Fraction(p.quantity) * multiplier * (1 if p.direction == "LONG" else -1)
            slope += units
            intercept -= units * mark
        slope_changes: dict[Fraction, Fraction] = {}
        for p in pending:
            units = Fraction(p.quantity) * multiplier
            entry = Fraction(p.entry_price)
            slope_changes[entry] = slope_changes.get(entry, Fraction(0)) - units
            if p.direction == "LONG":
                slope += units
                intercept -= units * min(mark, entry)
            else:
                intercept += units * max(0, mark - entry)

        lower = Fraction(0)
        roots = []
        for upper in (*sorted(slope_changes), None):
            if slope * direction > 0:
                root = -intercept / slope
                if 0 < root and lower <= root and (upper is None or root <= upper):
                    roots.append(root)
            if upper is None:
                break
            change = slope_changes[upper]
            slope += change
            intercept -= change * upper
            lower = upper
        if not roots:
            return None
        price = min(roots, key=lambda root: abs(root - mark))
        return Decimal(price.numerator) / Decimal(price.denominator)
