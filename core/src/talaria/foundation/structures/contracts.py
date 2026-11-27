"""统一市场结构（Market Structure）契约：4 种几何原语、检测产物、参数与反馈。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from talaria.foundation.key_levels.contracts import (
    EvidenceClass,
    KeyLevelParams,
    VwapAnchor,
    _parse_sigmas,
)
from talaria.foundation.types import ContractModel, DecimalStr

#: 算法版本；随每个结构特征与集合返回，便于复现与评估。
STRUCTURE_METHOD_VERSION = "structures@1"


class StructureFamily(StrEnum):
    """市场结构上位家族，对应几何形态与业务语义。"""

    LEVEL = "LEVEL"
    ZONE = "ZONE"
    TRENDLINE = "TRENDLINE"
    CHANNEL = "CHANNEL"
    CONFLUENCE = "CONFLUENCE"


class StructureCategory(StrEnum):
    """面向工作台与接口过滤的稳定结构大类。"""

    STRUCTURE = "STRUCTURE"
    SESSION = "SESSION"
    GAP = "GAP"
    ROUND_NUMBER = "ROUND_NUMBER"
    VOLUME = "VOLUME"
    LIQUIDATION = "LIQUIDATION"
    TREND = "TREND"
    CONFLUENCE = "CONFLUENCE"


class StructureStatus(StrEnum):
    """市场结构生命周期状态。"""

    ACTIVE = "ACTIVE"
    BROKEN = "BROKEN"
    FLIPPED = "FLIPPED"
    EXPIRED = "EXPIRED"


class PriceScaleMode(StrEnum):
    """斜向趋势线插值坐标系。"""

    LINEAR = "LINEAR"
    LOG = "LOG"


class AnchorSource(StrEnum):
    """趋势线锚点取价来源。"""

    WICK = "WICK"
    BODY = "BODY"


class StructureAnchor(ContractModel):
    """时间-价格坐标锚点。"""

    ts: int = Field(ge=0)
    price: DecimalStr


class AnalysisWindow(ContractModel):
    """计算该结构时实际观测的 K 线起止区间（Truth in Analysis）。"""

    from_ts: int = Field(ge=0)
    to_ts: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.from_ts > self.to_ts:
            raise ValueError("analysis_window.from_ts 不能大于 to_ts")
        return self


class HorizontalLineGeometry(ContractModel):
    """水平单价原语。"""

    kind: Literal["HORIZONTAL_LINE"] = "HORIZONTAL_LINE"
    price: DecimalStr


class HorizontalZoneGeometry(ContractModel):
    """水平区间带原语；可选起止时间戳（如 FVG / 时段区间）。"""

    kind: Literal["HORIZONTAL_ZONE"] = "HORIZONTAL_ZONE"
    price_low: DecimalStr
    price_high: DecimalStr
    start_ts: int | None = Field(default=None, ge=0)
    end_ts: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_zone(self) -> Self:
        if self.price_low > self.price_high:
            raise ValueError("price_low 不能大于 price_high")
        if self.start_ts is not None and self.end_ts is not None and self.start_ts > self.end_ts:
            raise ValueError("start_ts 不能大于 end_ts")
        return self


class SlopeLineGeometry(ContractModel):
    """斜向线段或射线原语，支持线性与对数坐标插值。"""

    kind: Literal["SLOPE_LINE"] = "SLOPE_LINE"
    anchor_start: StructureAnchor
    anchor_end: StructureAnchor
    scale: PriceScaleMode = PriceScaleMode.LINEAR
    extend_right: bool = True

    @model_validator(mode="after")
    def _check_slope(self) -> Self:
        if self.anchor_start.ts >= self.anchor_end.ts:
            raise ValueError("anchor_start.ts 必须严格早于 anchor_end.ts")
        if self.scale is PriceScaleMode.LOG and (
            self.anchor_start.price <= 0 or self.anchor_end.price <= 0
        ):
            raise ValueError("LOG 坐标下的趋势线锚点价格必须 > 0")
        return self


class ChannelGeometry(ContractModel):
    """平行或收敛双边通道原语。"""

    kind: Literal["CHANNEL"] = "CHANNEL"
    upper: SlopeLineGeometry
    lower: SlopeLineGeometry

    @model_validator(mode="after")
    def _check_channel(self) -> Self:
        if self.upper.scale is not self.lower.scale:
            raise ValueError("Channel 上下轨必须使用相同的坐标系")
        return self


StructureGeometry = Annotated[
    HorizontalLineGeometry | HorizontalZoneGeometry | SlopeLineGeometry | ChannelGeometry,
    Field(discriminator="kind"),
]

STRUCTURE_GEOMETRY_MODELS = (
    StructureAnchor,
    AnalysisWindow,
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    SlopeLineGeometry,
    ChannelGeometry,
)


class StructureFeature(ContractModel):
    """一条已确认、无前视的市场结构特征。"""

    structure_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    detector_id: str = Field(min_length=1, max_length=64)
    family: StructureFamily
    category: StructureCategory
    kind: str = Field(min_length=1, max_length=64)
    evidence_class: EvidenceClass = EvidenceClass.OBSERVED
    timeframe: str = Field(min_length=1, max_length=16)
    source_timeframe: str | None = Field(default=None, min_length=1, max_length=16)
    geometry: StructureGeometry
    strength: float = Field(ge=0, le=1)
    status: StructureStatus
    ts_available: int = Field(ge=0)
    formed_ts: int = Field(ge=0)
    analysis_window: AnalysisWindow
    touch_count: int = Field(default=0, ge=0)
    touch_timestamps: list[int] = Field(default_factory=list)
    last_touch_ts: int | None = Field(default=None, ge=0)
    volume_weight: DecimalStr = Decimal("0")
    constituent_ids: list[str] = Field(default_factory=list)
    method_version: str = STRUCTURE_METHOD_VERSION
    parameters: dict[str, str] = Field(default_factory=dict)


class StructureSet(ContractModel):
    """某个冻结快照与周期上的不可变市场结构集合。"""

    snapshot_id: str
    instrument_id: str
    timeframe: str
    cutoff_ts: int = Field(ge=0)
    as_of_ts: int = Field(ge=0)
    method_version: str = STRUCTURE_METHOD_VERSION
    detector_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    structures: list[StructureFeature] = Field(default_factory=list)


class StructureIgnoreRequest(ContractModel):
    """忽略/恢复单条系统市场结构；只写工作台状态库，不改冻结快照。"""

    instrument_id: str = Field(min_length=1, max_length=64)
    structure_id: str = Field(min_length=1, max_length=64)
    ignored: bool = True


class StructureIgnoreRecord(ContractModel):
    instrument_id: str
    structure_id: str
    ignored: bool
    updated_ts: int


class StructureIgnoreList(ContractModel):
    instrument_id: str
    entries: list[StructureIgnoreRecord] = Field(default_factory=list)


class StructureAdoptionRequest(ContractModel):
    """将一条系统市场结构采纳为独立人工画线（按几何原语映射）。"""

    snapshot_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=16)


class StructureAdoptionRecord(ContractModel):
    """采纳记录：仅追踪来源，生成后的人工画线与系统结构完全解耦。"""

    instrument_id: str
    structure_id: str
    snapshot_id: str
    drawing_id: str
    adopted_ts: int
    timeframe: str


class StructureFeedbackSummary(ContractModel):
    """单标的的市场结构人工反馈汇总。"""

    instrument_id: str
    adopted_structure_ids: list[str] = Field(default_factory=list)
    ignored_structure_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StructureParams:
    """市场结构引擎统一参数；全部序列化入结构 ``parameters`` 以保证可复现。"""

    # 水平关键位公共与迁移参数
    swing_span: int = 2
    confirm_bars: int = 2
    atr_period: int = 14
    pivot_atr_min: float = 0.6
    cluster_atr: float = 0.4
    touch_atr: float = 0.2
    gap_atr_min: float = 0.35
    opening_range_minutes: int = 30
    round_step: Decimal | None = None
    round_levels_per_side: int = 3
    round_window_atr: float = 25.0
    volume_profile_bins: int = 24
    value_area_ratio: float = 0.70
    vwap_anchor: VwapAnchor = VwapAnchor.SESSION
    vwap_anchor_ts: int | None = None
    vwap_band_sigmas: str = "1,2"
    leverage_tiers: tuple[int, ...] = (5, 10, 25, 50, 100)
    liquidation_cluster_atr: float = 0.5
    liquidation_min_points: int = 4
    liquidation_max_levels: int = 8
    liquidation_min_oi_change: float = 0.0005
    liquidation_max_distance_atr: float = 12.0
    max_levels: int = 60

    # SMC 增量参数
    fvg_atr_min: float = 0.3
    equal_level_atr: float = 0.15
    equal_pullback_atr: float = 1.0

    # 自动趋势线参数
    trendline_lookback_bars: int = 250
    trendline_spans: tuple[int, ...] = (2, 5)
    trendline_max_pivots: int = 40
    trendline_scale: PriceScaleMode = PriceScaleMode.LINEAR
    trendline_anchor_source: AnchorSource = AnchorSource.WICK
    island_gap_atr: float = 2.0
    respect_islands: bool = True
    min_anchor_span: int = 5
    max_body_pierce_atr: float = 0.05
    max_wick_pierce_atr: float = 0.30
    max_pierce_ratio: float = 0.05
    min_touch_gap: int = 3
    trendline_touch_atr: float = 0.25
    trendline_break_atr: float = 0.25
    trendline_recency_tau: int = 80
    trendline_dedup_slope_atr: float = 0.04
    trendline_dedup_price_atr: float = 0.50
    max_trendlines_per_side: int = 3
    envelope_window: int = 30

    # 多源共振聚合带参数
    confluence_bandwidth_atr: float = 0.45
    confluence_min_constituents: int = 2
    confluence_max_zones: int = 12

    def __post_init__(self) -> None:
        if not isinstance(self.vwap_anchor, VwapAnchor):
            object.__setattr__(self, "vwap_anchor", VwapAnchor(str(self.vwap_anchor).upper()))
        if not isinstance(self.trendline_scale, PriceScaleMode):
            object.__setattr__(
                self, "trendline_scale", PriceScaleMode(str(self.trendline_scale).upper())
            )
        if not isinstance(self.trendline_anchor_source, AnchorSource):
            object.__setattr__(
                self,
                "trendline_anchor_source",
                AnchorSource(str(self.trendline_anchor_source).upper()),
            )
        # 复用 KeyLevelParams 校验水平位参数约束
        self.to_key_level_params()
        if self.fvg_atr_min <= 0 or self.equal_level_atr <= 0 or self.equal_pullback_atr <= 0:
            raise ValueError("SMC ATR 阈值必须为正数")
        if self.trendline_lookback_bars < 10 or self.trendline_max_pivots < 2:
            raise ValueError("trendline_lookback_bars >= 10 且 trendline_max_pivots >= 2")
        if not self.trendline_spans or any(span < 1 for span in self.trendline_spans):
            raise ValueError("trendline_spans 必须为正整数序列")
        if self.min_anchor_span < 1 or self.min_touch_gap < 1 or self.trendline_recency_tau < 1:
            raise ValueError("min_anchor_span / min_touch_gap / trendline_recency_tau 必须 >= 1")
        if self.max_body_pierce_atr < 0 or self.max_wick_pierce_atr < 0:
            raise ValueError("穿透容差不能为负")
        if not 0.0 <= self.max_pierce_ratio <= 1.0:
            raise ValueError("max_pierce_ratio 必须在 [0, 1]")
        if self.max_trendlines_per_side < 1 or self.envelope_window < 5:
            raise ValueError("max_trendlines_per_side >= 1 且 envelope_window >= 5")
        if self.confluence_bandwidth_atr <= 0 or self.confluence_min_constituents < 2:
            raise ValueError("confluence_bandwidth_atr > 0 且 confluence_min_constituents >= 2")
        if self.confluence_max_zones < 1:
            raise ValueError("confluence_max_zones 必须 >= 1")

    def to_key_level_params(self) -> KeyLevelParams:
        """投影为旧关键位引擎参数对象，供水平基础检测器无缝复用。"""
        return KeyLevelParams(
            swing_span=self.swing_span,
            confirm_bars=self.confirm_bars,
            atr_period=self.atr_period,
            pivot_atr_min=self.pivot_atr_min,
            cluster_atr=self.cluster_atr,
            touch_atr=self.touch_atr,
            gap_atr_min=self.gap_atr_min,
            opening_range_minutes=self.opening_range_minutes,
            round_step=self.round_step,
            round_levels_per_side=self.round_levels_per_side,
            round_window_atr=self.round_window_atr,
            volume_profile_bins=self.volume_profile_bins,
            value_area_ratio=self.value_area_ratio,
            vwap_anchor=self.vwap_anchor,
            vwap_anchor_ts=self.vwap_anchor_ts,
            vwap_band_sigmas=self.vwap_band_sigmas,
            leverage_tiers=self.leverage_tiers,
            liquidation_cluster_atr=self.liquidation_cluster_atr,
            liquidation_min_points=self.liquidation_min_points,
            liquidation_max_levels=self.liquidation_max_levels,
            liquidation_min_oi_change=self.liquidation_min_oi_change,
            liquidation_max_distance_atr=self.liquidation_max_distance_atr,
            max_levels=self.max_levels,
        )

    def as_parameters(self) -> dict[str, str]:
        merged = self.to_key_level_params().as_parameters()
        merged.update(
            {
                "fvg_atr_min": f"{self.fvg_atr_min:g}",
                "equal_level_atr": f"{self.equal_level_atr:g}",
                "equal_pullback_atr": f"{self.equal_pullback_atr:g}",
                "trendline_lookback_bars": str(self.trendline_lookback_bars),
                "trendline_spans": ",".join(str(s) for s in self.trendline_spans),
                "trendline_max_pivots": str(self.trendline_max_pivots),
                "trendline_scale": self.trendline_scale.value,
                "trendline_anchor_source": self.trendline_anchor_source.value,
                "island_gap_atr": f"{self.island_gap_atr:g}",
                "respect_islands": "true" if self.respect_islands else "false",
                "min_anchor_span": str(self.min_anchor_span),
                "max_body_pierce_atr": f"{self.max_body_pierce_atr:g}",
                "max_wick_pierce_atr": f"{self.max_wick_pierce_atr:g}",
                "max_pierce_ratio": f"{self.max_pierce_ratio:g}",
                "min_touch_gap": str(self.min_touch_gap),
                "trendline_touch_atr": f"{self.trendline_touch_atr:g}",
                "trendline_break_atr": f"{self.trendline_break_atr:g}",
                "trendline_recency_tau": str(self.trendline_recency_tau),
                "trendline_dedup_slope_atr": f"{self.trendline_dedup_slope_atr:g}",
                "trendline_dedup_price_atr": f"{self.trendline_dedup_price_atr:g}",
                "max_trendlines_per_side": str(self.max_trendlines_per_side),
                "envelope_window": str(self.envelope_window),
                "confluence_bandwidth_atr": f"{self.confluence_bandwidth_atr:g}",
                "confluence_min_constituents": str(self.confluence_min_constituents),
                "confluence_max_zones": str(self.confluence_max_zones),
                # 校验 sigmas 格式
                "vwap_band_sigmas": ",".join(
                    f"{sigma:g}" for sigma in _parse_sigmas(self.vwap_band_sigmas)
                ),
            }
        )
        return merged
