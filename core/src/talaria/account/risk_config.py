"""交易方案中的子账户资金和默认定仓、风控参数。"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from talaria.foundation.paper import TrailingStopSpec
from talaria.foundation.types import ContractModel, DecimalStr

type Positive = Annotated[DecimalStr, Field(strict=False, gt=0)]
type Rate = Annotated[DecimalStr, Field(strict=False, ge=0, lt=1)]
type RiskPercent = Annotated[DecimalStr, Field(strict=False, gt=0, lt=1)]


class QuietWindow(ContractModel):
    model_config = ConfigDict(frozen=True, strict=True)

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    instrument_id: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("静默窗口须满足 start < end")
        return self


class AccountRiskConfig(ContractModel):
    model_config = ConfigDict(frozen=True, strict=True)

    initial_capital: Positive = Decimal("10000")
    currency: str = Field(default="USDT", min_length=1)
    margin_mode: Literal["ISOLATED", "CROSS"] = "CROSS"
    leverage: Annotated[DecimalStr, Field(strict=False, ge=1)] = Decimal(5)
    trading_state: Literal["ACTIVE", "REDUCING", "HALTED"] = "ACTIVE"
    r_unit_pct: RiskPercent = Decimal("0.005")
    max_single_r_pct: RiskPercent = Decimal("0.01")
    cluster_max_r: Positive = Decimal(3)
    correlation_clusters: dict[str, str] = Field(default_factory=dict)
    friction_buffer_pct: Rate = Decimal("0.002")
    daily_soft_drawdown_pct: RiskPercent = Decimal("0.05")
    daily_hard_drawdown_pct: RiskPercent = Decimal("0.10")
    peak_soft_drawdown_pct: RiskPercent = Decimal("0.10")
    peak_hard_drawdown_pct: RiskPercent = Decimal("0.20")
    liquidate_on_halt: bool = False
    mark_max_age_ms: int = Field(default=900_000, gt=0)
    liquidation_buffer_pct: Rate = Decimal(0)
    liquidation_buffer_ticks: int = Field(default=1, ge=0)
    taker_fee_pct: Rate = Decimal("0.0006")
    slippage_pct: Rate = Decimal("0.0004")
    funding_rate_buffer: Rate = Decimal("0.0001")
    max_holding_ms: int = Field(default=86_400_000, gt=0, le=2**63 - 1)
    trailing_stop: TrailingStopSpec | None = None
    min_net_rr: Positive = Decimal("1.5")
    quiet_windows: Annotated[tuple[QuietWindow, ...], Field(strict=False)] = ()

    @model_validator(mode="after")
    def _valid_bounds(self) -> Self:
        if self.r_unit_pct > self.max_single_r_pct:
            raise ValueError("r_unit_pct 不得超过 max_single_r_pct")
        if not (
            self.daily_soft_drawdown_pct < self.daily_hard_drawdown_pct
            and self.peak_soft_drawdown_pct < self.peak_hard_drawdown_pct
        ):
            raise ValueError("软回撤阈值须低于对应硬阈值")
        return self
