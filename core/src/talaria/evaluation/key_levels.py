"""关键位离线质量评估：逐时点重放引擎、统计触及后的 ATR 反应并与随机价位对照。

评估语义（任务单 12）：

- **时点**：窗口 ``[from_ts, to_ts]`` 内每根已闭合柱都产生一个 cutoff，cutoff 取
  该柱的 ``ts_available``。每次只把 ``ts_available <= cutoff`` 的柱子交给
  :func:`talaria.foundation.key_levels.compute_key_levels`，因此不使用未来柱。
- **样本**：每个在不同 cutoff 上重复出现的 ``level_id`` 只计一次，取其首次被发现
  时、窗口内第一次被触及的时点。若首次触及后数据不足 ``horizon_bars`` 根，样本
  记为 ``skipped``，不以 0 代替。
- **反应**：触及后 ``horizon_bars`` 根内，若任一收盘价离开该关键位（线：绝对距离；
  区间：到区间边界的距离）达到 ``atr_multiple * ATR(atr_period)``，记为有反应；
  向上突破与向下反弹都算反应。偏离幅度以 ATR 为单位取窗口内最大值。
- **随机基线**：在同一窗口的收盘价分布中按固定种子抽取 ``baseline_samples`` 个
  随机价位，用完全相同的触及与反应判定；基线不读取引擎的 ``level_id``、状态或
  强度。每个 ``kind`` 使用由 ``seed`` 与类别名派生的独立随机流，保证可复现。
- **类别**：输出按 :func:`registered_level_kinds` 枚举，新增注册类别无需修改本模块。

ATR 复用 :mod:`talaria.foundation.operators` 的 ATR（默认周期 14，Wilder 平滑）；
预热不足的样本计入 ``skipped``。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.connector import RawBar
from talaria.foundation.key_levels import (
    KEY_LEVEL_METHOD_VERSION,
    KeyLevel,
    KeyLevelKind,
    KeyLevelParams,
    SessionProfile,
    compute_key_levels,
)
from talaria.foundation.operators import IndicatorName, IndicatorSpec, compute_series
from talaria.foundation.timeframes import timeframe_ms

DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_MULTIPLE = 1.0
DEFAULT_HORIZON_BARS = 20
DEFAULT_BASELINE_SAMPLES = 200
DEFAULT_SEED = 42
DEFAULT_NAIVE_TIMEZONE = "Asia/Shanghai"

#: 预热窗口：至少给 ATR 留出 4 个完整周期。
_WARMUP_PERIODS = 4

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_EPOCH_MS_RE = re.compile(r"^-?\d+$")

REACTION_DEFINITION = (
    "reaction: within horizon_bars bars strictly after the touch bar, a close moves at least "
    "atr_multiple * ATR(atr_period) away from the level (line: |close-price|; zone: distance "
    "to the zone); both directions count. excursion_atr is the maximum such distance divided "
    "by the ATR at the touch bar."
)


class EvaluationError(ValueError):
    """评估参数或输入错误；CLI 会转换成带错误码的 JSON。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _utc_iso(timestamp_ms: int) -> str:
    moment = _EPOCH + timedelta(milliseconds=timestamp_ms)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time_ms(value: str | int) -> int:
    """解析毫秒时间戳或 ISO-8601 时间。

    不带时区的 ISO 字符串按 ``Asia/Shanghai`` 解释；带 ``Z`` 或偏移量时按其自身
    时区换算到 UTC 毫秒。非法输入抛 :class:`EvaluationError`（``ValueError`` 子类）。
    """
    if isinstance(value, bool):
        raise EvaluationError("invalid_time", f"非法时间: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if _EPOCH_MS_RE.match(text):
        return int(text)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvaluationError("invalid_time", f"无法解析时间: {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(DEFAULT_NAIVE_TIMEZONE))
    delta = moment.astimezone(timezone.utc) - _EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


@dataclass(frozen=True, slots=True)
class KeyLevelEvaluationConfig:
    """一次离线评估的输入；所有随机性由 ``seed`` 固定。"""

    instrument_id: str
    timeframe: str
    from_ts: int
    to_ts: int
    atr_multiple: float = DEFAULT_ATR_MULTIPLE
    horizon_bars: int = DEFAULT_HORIZON_BARS
    baseline_samples: int = DEFAULT_BASELINE_SAMPLES
    seed: int = DEFAULT_SEED
    atr_period: int = DEFAULT_ATR_PERIOD
    params: KeyLevelParams | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise EvaluationError("invalid_argument", "instrument_id 不能为空")
        try:
            timeframe_ms(self.timeframe)
        except KeyError as exc:
            raise EvaluationError(
                "unsupported_timeframe", f"不支持的周期: {self.timeframe!r}"
            ) from exc
        if self.from_ts > self.to_ts:
            raise EvaluationError("invalid_argument", "from_ts 不能晚于 to_ts")
        if not math.isfinite(self.atr_multiple) or self.atr_multiple <= 0:
            raise EvaluationError("invalid_argument", "atr_multiple 必须为正数")
        if self.horizon_bars <= 0:
            raise EvaluationError("invalid_argument", "horizon_bars 必须为正整数")
        if self.baseline_samples < 0:
            raise EvaluationError("invalid_argument", "baseline_samples 不能为负")
        if self.atr_period <= 1:
            raise EvaluationError("invalid_argument", "atr_period 必须大于 1")


@dataclass(frozen=True, slots=True)
class KindEvaluation:
    """单个关键位类别的样本与随机基线对照。"""

    kind: str
    sample_count: int
    reactions: int
    mean_reaction_atr: float | None
    skipped_count: int
    baseline_sample_count: int
    baseline_reactions: int
    baseline_mean_reaction_atr: float | None
    baseline_skipped_count: int

    @property
    def reaction_rate(self) -> float | None:
        return None if self.sample_count == 0 else self.reactions / self.sample_count

    @property
    def baseline_reaction_rate(self) -> float | None:
        if self.baseline_sample_count == 0:
            return None
        return self.baseline_reactions / self.baseline_sample_count

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "sample_count": self.sample_count,
            "reaction_count": self.reactions,
            "reaction_rate": _rounded(self.reaction_rate),
            "mean_reaction_atr": _rounded(self.mean_reaction_atr),
            "skipped_count": self.skipped_count,
            "baseline_sample_count": self.baseline_sample_count,
            "baseline_reaction_count": self.baseline_reactions,
            "baseline_reaction_rate": _rounded(self.baseline_reaction_rate),
            "baseline_mean_reaction_atr": _rounded(self.baseline_mean_reaction_atr),
            "baseline_skipped_count": self.baseline_skipped_count,
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """含参数、全部类别对照与跳过计数的机器可读报告。"""

    config: KeyLevelEvaluationConfig
    results: tuple[KindEvaluation, ...]
    skipped: int
    baseline_skipped: int

    @property
    def total_sample_count(self) -> int:
        return sum(row.sample_count for row in self.results)

    @property
    def baseline_sample_count(self) -> int:
        return sum(row.baseline_sample_count for row in self.results)

    def as_dict(self) -> dict[str, object]:
        config = self.config
        return {
            "instrument_id": config.instrument_id,
            "timeframe": config.timeframe,
            "from_ts": config.from_ts,
            "to_ts": config.to_ts,
            "from_iso": _utc_iso(config.from_ts),
            "to_iso": _utc_iso(config.to_ts),
            "atr_period": config.atr_period,
            "atr_multiple": config.atr_multiple,
            "horizon_bars": config.horizon_bars,
            "baseline_samples": config.baseline_samples,
            "seed": config.seed,
            "method_version": KEY_LEVEL_METHOD_VERSION,
            "reaction_definition": REACTION_DEFINITION,
            "skipped": self.skipped,
            "baseline_skipped": self.baseline_skipped,
            "total_sample_count": self.total_sample_count,
            "baseline_sample_count": self.baseline_sample_count,
            "results": [row.as_dict() for row in self.results],
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def registered_level_kinds() -> tuple[str, ...]:
    """返回引擎契约中全部已注册的关键位类别。

    以 :class:`KeyLevelKind` 为唯一注册表；新增枚举值自动进入评估输出，评估代码
    不需要按类别分支。
    """
    return tuple(kind.value for kind in KeyLevelKind)


def _price_distance(close: Decimal, target: Decimal | KeyLevel) -> Decimal:
    if isinstance(target, KeyLevel):
        if target.price is not None:
            return abs(close - target.price)
        low = target.price_low
        high = target.price_high
        assert low is not None and high is not None
        if close < low:
            return low - close
        if close > high:
            return close - high
        return Decimal(0)
    return abs(close - target)


def _touch_bar(bar: CompactBar, target: Decimal | KeyLevel) -> bool:
    if bar.high is None or bar.low is None:
        return False
    if isinstance(target, KeyLevel):
        if target.price is not None:
            return bar.low <= target.price <= bar.high
        assert target.price_low is not None and target.price_high is not None
        return bar.low <= target.price_high and bar.high >= target.price_low
    return bar.low <= target <= bar.high


@dataclass(frozen=True, slots=True)
class _Reaction:
    """一次触及后的最大偏离（ATR 倍数）与是否越过阈值。"""

    excursion_atr: Decimal
    reacted: bool


def _measure_reaction(
    bars: Sequence[CompactBar],
    atr_values: Sequence[Decimal | None],
    touch_index: int,
    target: Decimal | KeyLevel,
    *,
    horizon_bars: int,
    atr_multiple: Decimal,
) -> _Reaction | None:
    atr = atr_values[touch_index]
    if atr is None or atr <= 0:
        return None
    if touch_index + horizon_bars >= len(bars):
        return None
    excursion = Decimal(0)
    observed = 0
    for bar in bars[touch_index + 1 : touch_index + horizon_bars + 1]:
        if bar.close is None:
            continue
        observed += 1
        excursion = max(excursion, _price_distance(bar.close, target))
    if observed == 0:
        return None
    return _Reaction(
        excursion_atr=excursion / atr,
        reacted=excursion >= atr * atr_multiple,
    )


def _baseline_seed(seed: int, kind: str) -> int:
    digest = hashlib.sha256(f"{seed}:{kind}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _first_touch(
    bars: Sequence[CompactBar],
    start_index: int,
    target: Decimal | KeyLevel,
    *,
    until_ts: int,
) -> int | None:
    for index in range(start_index, len(bars)):
        bar = bars[index]
        if bar.timestamp > until_ts:
            return None
        if _touch_bar(bar, target):
            return index
    return None


def _summarize(
    kind: str,
    reactions: list[_Reaction],
    skipped_count: int,
    baseline_reactions: list[_Reaction],
    baseline_skipped_count: int,
) -> KindEvaluation:
    def mean(values: list[_Reaction]) -> float | None:
        if not values:
            return None
        return float(sum(value.excursion_atr for value in values) / len(values))

    return KindEvaluation(
        kind=kind,
        sample_count=len(reactions),
        reactions=sum(1 for value in reactions if value.reacted),
        mean_reaction_atr=mean(reactions),
        skipped_count=skipped_count,
        baseline_sample_count=len(baseline_reactions),
        baseline_reactions=sum(1 for value in baseline_reactions if value.reacted),
        baseline_mean_reaction_atr=mean(baseline_reactions),
        baseline_skipped_count=baseline_skipped_count,
    )


def evaluate_key_levels(
    bars: Sequence[CompactBar],
    config: KeyLevelEvaluationConfig,
    *,
    session_profile: SessionProfile | None = None,
) -> EvaluationReport:
    """逐时点评估 ``bars`` 在 ``config`` 窗口内的关键位反应。

    ``bars`` 可以包含窗口之后的数据（用于计算触及后的反应），但每次调用引擎前
    都会按 cutoff 过滤，保证无前视。窗口内没有已闭合柱时返回全零报告，而不是
    报错。
    """
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    atr_state = compute_series(
        ordered, IndicatorSpec(name=IndicatorName.ATR, period=config.atr_period)
    )
    atr_values: tuple[Decimal | None, ...] = tuple(
        point.get("value") for point in atr_state.points
    )

    window_indices = [
        index
        for index, bar in enumerate(ordered)
        if config.from_ts <= bar.timestamp <= config.to_ts
    ]
    cutoff_indices = [
        index
        for index in window_indices
        if ordered[index].status is not BarStatus.FEED_GAP
    ]

    discovered: dict[str, KeyLevel] = {}
    for index in cutoff_indices:
        cutoff = ordered[index].ts_available
        visible = [bar for bar in ordered if bar.ts_available <= cutoff]
        for level in compute_key_levels(
            instrument_id=config.instrument_id,
            timeframe=config.timeframe,
            bars=visible,
            cutoff_ts=cutoff,
            session_profile=session_profile,
            params=config.params,
        ):
            if level.ts_available > cutoff:
                continue
            discovered.setdefault(level.level_id, level)

    kinds = list(registered_level_kinds())
    seen = set(kinds)
    for level in discovered.values():
        if level.kind.value not in seen:
            kinds.append(level.kind.value)
            seen.add(level.kind.value)

    real: dict[str, list[_Reaction]] = {kind: [] for kind in kinds}
    real_skipped: dict[str, int] = {kind: 0 for kind in kinds}
    baseline: dict[str, list[_Reaction]] = {kind: [] for kind in kinds}
    baseline_skipped: dict[str, int] = {kind: 0 for kind in kinds}
    multiple = Decimal(str(config.atr_multiple))

    closes = [
        ordered[index].close
        for index in window_indices
        if ordered[index].close is not None
    ]

    for level in discovered.values():
        kind = level.kind.value
        start = window_indices[0]
        while start < len(ordered) and ordered[start].ts_available <= level.ts_available:
            start += 1
        touch = _first_touch(ordered, start, level, until_ts=config.to_ts)
        if touch is None:
            continue
        reaction = _measure_reaction(
            ordered,
            atr_values,
            touch,
            level,
            horizon_bars=config.horizon_bars,
            atr_multiple=multiple,
        )
        if reaction is None:
            real_skipped[kind] += 1
            continue
        real[kind].append(reaction)

    if config.baseline_samples and closes and window_indices:
        for kind in kinds:
            rng = random.Random(_baseline_seed(config.seed, kind))
            for _ in range(config.baseline_samples):
                price = closes[rng.randrange(len(closes))]
                baseline_touch = _first_touch(
                    ordered,
                    window_indices[0],
                    price,
                    until_ts=config.to_ts,
                )
                if baseline_touch is None:
                    baseline_skipped[kind] += 1
                    continue
                baseline_reaction = _measure_reaction(
                    ordered,
                    atr_values,
                    baseline_touch,
                    price,
                    horizon_bars=config.horizon_bars,
                    atr_multiple=multiple,
                )
                if baseline_reaction is None:
                    baseline_skipped[kind] += 1
                    continue
                baseline[kind].append(baseline_reaction)

    results = tuple(
        _summarize(
            kind,
            real[kind],
            real_skipped[kind],
            baseline[kind],
            baseline_skipped[kind],
        )
        for kind in kinds
    )
    return EvaluationReport(
        config=config,
        results=results,
        skipped=sum(real_skipped.values()),
        baseline_skipped=sum(baseline_skipped.values()),
    )


def load_compact_bars(
    *,
    instrument_symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    raw_archive_id: str,
    fixture_dir: Path | None = None,
    data_dir: Path | None = None,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    atr_period: int = DEFAULT_ATR_PERIOD,
) -> tuple[list[CompactBar], str]:
    """从 fixture 或 RawStore 读取并压缩评估所需柱子。

    优先使用 ``fixture_dir``；给它时读 ``<fixture_dir>/<SYMBOL>/<timeframe>.json``。
    未给 ``fixture_dir`` 或该文件不可用时，回退到 ``data_dir`` 下的 ``RawStore``。
    返回柱子与数据来源描述。
    """
    from talaria.packs.testing import FixtureConnector
    from talaria.perception.compaction import compact_series
    from talaria.perception.raw_store import RawStore

    interval = timeframe_ms(timeframe)
    warmup = _WARMUP_PERIODS * (atr_period + 1)
    fetch_end = to_ts + horizon_bars * interval
    limit = max(1, (to_ts - from_ts) // interval + 1 + warmup)

    raw: list[RawBar] = []
    source = ""
    if fixture_dir is not None:
        fixture_file = Path(fixture_dir) / instrument_symbol / f"{timeframe}.json"
        if fixture_file.is_file():
            connector = FixtureConnector(Path(fixture_dir))
            raw = list(
                connector.fetch_bars(
                    symbol=instrument_symbol,
                    timeframe=timeframe,
                    end_ts=fetch_end,
                    limit=limit,
                )
            )
            source = f"fixture:{fixture_file}"

    if not raw and data_dir is not None:
        raw_store = RawStore(data_dir, raw_archive_id=raw_archive_id)
        raw = [
            bar
            for bar in raw_store.read(instrument_symbol, timeframe)
            if bar.ts_event <= fetch_end
        ]
        source = f"raw-store:{Path(data_dir)}"

    if not raw:
        raise EvaluationError(
            "data_unavailable",
            f"{instrument_symbol} {timeframe} 没有可用数据"
            "（检查 --fixture-dir 或 --data-dir）",
        )

    compact = compact_series(raw, timeframe=timeframe, cutoff_ts=fetch_end)
    earliest = from_ts - warmup * interval
    selected = [
        bar
        for bar in compact
        if bar.timestamp >= earliest and bar.timestamp <= fetch_end
    ]
    if not any(from_ts <= bar.timestamp <= to_ts for bar in selected):
        raise EvaluationError(
            "no_data",
            f"时间范围内没有柱子: {_utc_iso(from_ts)} ~ {_utc_iso(to_ts)}",
        )
    return selected, source


def format_table(report: EvaluationReport) -> str:
    """把报告渲染为供人阅读的定宽表格。"""
    header = (
        f"{'kind':<20} {'samples':>7} {'reaction':>8} {'mean_atr':>8} "
        f"{'skipped':>7} {'base_n':>7} {'base_rate':>9} {'base_atr':>8}"
    )
    lines = [
        f"instrument={report.config.instrument_id} timeframe={report.config.timeframe} "
        f"window={_utc_iso(report.config.from_ts)}~{_utc_iso(report.config.to_ts)} "
        f"atr_multiple={report.config.atr_multiple} horizon={report.config.horizon_bars} "
        f"seed={report.config.seed}",
        header,
    ]

    def fmt(value: float | None, digits: int = 4) -> str:
        return "-" if value is None else f"{value:.{digits}f}"

    for row in report.results:
        lines.append(
            f"{row.kind:<20} {row.sample_count:>7} "
            f"{fmt(row.reaction_rate):>8} {fmt(row.mean_reaction_atr):>8} "
            f"{row.skipped_count:>7} {row.baseline_sample_count:>7} "
            f"{fmt(row.baseline_reaction_rate):>9} "
            f"{fmt(row.baseline_mean_reaction_atr):>8}"
        )
    lines.append(
        f"total_samples={report.total_sample_count} skipped={report.skipped} "
        f"baseline_skipped={report.baseline_skipped}"
    )
    return "\n".join(lines)


def dumps_report(report: EvaluationReport, *, source: str | None = None) -> str:
    """序列化为 JSON 文本，保持报告字段的声明顺序。"""
    payload = report.as_dict()
    if source is not None:
        payload["source"] = source
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
