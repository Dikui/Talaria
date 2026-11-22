"""线性永续的研究估值模型，使用固定维持保证金率。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from talaria.foundation.margin import Direction, MarginEstimate, MarginPortfolio
from talaria.foundation.market import MarginTrait


@dataclass(frozen=True, slots=True)
class CryptoPerpMarginValuation:
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
        equity = portfolio.collateral
        initial = maintenance = Decimal(0)
        for position in portfolio.positions:
            spec = self.margin_trait(position.instrument_id)
            if not 1 <= portfolio.leverage <= spec.max_leverage:
                raise ValueError(f"{position.instrument_id} 杠杆超出市场约束")
            mark = mark_prices.get(position.instrument_id)
            if mark is None or not mark.is_finite() or mark <= 0:
                raise ValueError(f"缺少有效标记价：{position.instrument_id}")
            units = position.quantity * spec.contract_multiplier
            notional = mark * units
            equity += (mark - position.entry_price) * units * (1 if position.direction == "LONG" else -1)
            initial += notional / portfolio.leverage
            maintenance += notional * self.maintenance_rate
        return MarginEstimate(equity=equity, initial_margin=initial, maintenance_margin=maintenance)

    def cross_liquidation_price(
        self, portfolio: MarginPortfolio, mark_prices: Mapping[str, Decimal],
        instrument_id: str, *, as_of: int,
    ) -> Decimal | None:
        estimate = self.cross_margin(portfolio, mark_prices, as_of=as_of)
        units = sum((
            p.quantity * ((1 if p.direction == "LONG" else -1) - self.maintenance_rate)
            for p in portfolio.positions if p.instrument_id == instrument_id
        ), Decimal(0)) * self.margin_trait(instrument_id).contract_multiplier
        if not units:
            return None
        price = mark_prices[instrument_id] - (estimate.equity - estimate.maintenance_margin) / units
        return price if price > 0 else None
