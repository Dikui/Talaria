"""Abstain 判定与宿主输出解析。

这里的判定全部是纯函数：不读时钟、不访问网络、不写状态；服务层负责传入
``now_ms`` 与冻结后的会诊包，因此同一条输出在任何时刻都能复现同一结论。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from talaria.foundation.collaboration.contracts import (
    Abstain,
    AbstainReason,
    ConsultationPackage,
    EvidenceKind,
    EvidenceRef,
    SetupDirection,
    SetupInsight,
)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _snapshot_evidence(package: ConsultationPackage) -> list[EvidenceRef]:
    for ref in package.evidence_catalog:
        if ref.kind is EvidenceKind.SNAPSHOT:
            return [ref]
    return [
        EvidenceRef(
            evidence_id=f"snapshot:{package.snapshot_id}",
            kind=EvidenceKind.SNAPSHOT,
            label=f"快照 {package.snapshot_id}",
        )
    ]


def stale_abstain(
    package: ConsultationPackage,
    *,
    now_ms: int,
    stale_after_ms: int,
) -> Abstain | None:
    """快照截止时间过旧时拒绝分析；不把缺失/未知时间当成新鲜。"""

    age_ms = now_ms - package.snapshot_cutoff_ts
    if age_ms <= stale_after_ms:
        return None
    minutes = max(age_ms, 0) // 60_000
    return Abstain(
        reason_code=AbstainReason.STALE_SNAPSHOT,
        message=(
            f"STALE_SNAPSHOT：快照 {package.snapshot_id} 已过期"
            f"（截止时间距今约 {minutes} 分钟，超过 {stale_after_ms // 60_000} 分钟"
            "上限）；请先重新冻结快照。"
        ),
        evidence_refs=_snapshot_evidence(package),
    )


def _geometry_problem(insight: SetupInsight) -> str | None:
    if insight.direction is SetupDirection.LONG:
        if insight.invalidation_stop >= insight.entry_low:
            return "LONG 的止损必须低于入场带下沿"
        if any(target <= insight.entry_high for target in insight.take_profit):
            return "LONG 的止盈必须高于入场带上沿"
    elif insight.direction is SetupDirection.SHORT:
        if insight.invalidation_stop <= insight.entry_high:
            return "SHORT 的止损必须高于入场带上沿"
        if any(target >= insight.entry_low for target in insight.take_profit):
            return "SHORT 的止盈必须低于入场带下沿"
    return None


def _trade_band(insight: SetupInsight) -> tuple[Decimal, Decimal]:
    prices = [
        insight.entry_low,
        insight.entry_high,
        insight.invalidation_stop,
        *insight.take_profit,
    ]
    return min(prices), max(prices)


def _catalog_ref(
    ref: EvidenceRef,
    catalog: dict[str, EvidenceRef],
) -> EvidenceRef:
    known = catalog.get(ref.evidence_id)
    if known is not None:
        return known
    return ref


def canonicalize_insight(
    package: ConsultationPackage,
    insight: SetupInsight,
) -> SetupInsight:
    """把模型给的证据引用替换为冻结包里的权威副本，防止标签/价格被改写。"""

    catalog = {ref.evidence_id: ref for ref in package.evidence_catalog}
    refs: list[EvidenceRef] = []
    seen: set[str] = set()
    for ref in insight.evidence_refs:
        if ref.evidence_id in seen:
            continue
        seen.add(ref.evidence_id)
        refs.append(_catalog_ref(ref, catalog))
    return insight.model_copy(update={"evidence_refs": refs})


def evaluate_insight(
    package: ConsultationPackage,
    insight: SetupInsight,
) -> Abstain | None:
    """在落库/展示前检查几何、证据解析与价格区间一致性。"""

    problem = _geometry_problem(insight)
    if problem is not None:
        return Abstain(
            reason_code=AbstainReason.GEOMETRY_INVERTED,
            message=f"几何关系倒挂：{problem}；已拒绝该 Setup Insight。",
            evidence_refs=list(insight.evidence_refs),
        )

    catalog = {ref.evidence_id: ref for ref in package.evidence_catalog}
    unknown = [
        ref for ref in insight.evidence_refs if ref.evidence_id not in catalog
    ]
    if unknown:
        return Abstain(
            reason_code=AbstainReason.EVIDENCE_CONFLICT,
            message=(
                "关键证据冲突：以下证据 ID 不在冻结快照的证据目录中，"
                f"无法核对：{', '.join(ref.evidence_id for ref in unknown)}"
            ),
            evidence_refs=unknown,
        )

    if not insight.evidence_refs:
        return Abstain(
            reason_code=AbstainReason.EVIDENCE_CONFLICT,
            message="关键证据冲突：结论没有引用任何工具结果、画线或关键位。",
            evidence_refs=[],
        )

    canonical = [_catalog_ref(ref, catalog) for ref in insight.evidence_refs]
    trade_low, trade_high = _trade_band(insight)

    if package.selection is not None:
        selection = package.selection
        if (
            insight.entry_high < selection.price_min
            or insight.entry_low > selection.price_max
        ):
            return Abstain(
                reason_code=AbstainReason.EVIDENCE_CONFLICT,
                message=(
                    "关键证据冲突：入场带与框选价格区间不相交，"
                    "会诊包与结论描述的不是同一段行情。"
                ),
                evidence_refs=canonical,
            )

    if (
        insight.entry_high < package.visible.price_min
        or insight.entry_low > package.visible.price_max
    ):
        return Abstain(
            reason_code=AbstainReason.EVIDENCE_CONFLICT,
            message="关键证据冲突：入场带落在只读工具返回的可视价格区间之外。",
            evidence_refs=canonical,
        )

    mismatched = [
        ref
        for ref in canonical
        if ref.price_low is not None
        and ref.price_high is not None
        and (ref.price_high < trade_low or ref.price_low > trade_high)
    ]
    if mismatched:
        return Abstain(
            reason_code=AbstainReason.EVIDENCE_CONFLICT,
            message=(
                "关键证据冲突：引用的关键位/画线价格与止损到止盈的交易带不相交："
                + "、".join(ref.evidence_id for ref in mismatched)
            ),
            evidence_refs=mismatched,
        )

    return None


def _json_object(text: str) -> dict[str, object]:
    stripped = text.strip()
    candidates = [stripped]
    fenced = _FENCE.search(stripped)
    if fenced is not None:
        candidates.append(fenced.group(1))
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("宿主输出不是合法 JSON 对象")


def parse_host_analysis(text: str) -> SetupInsight | Abstain:
    """解析宿主最终消息；结构不合法时抛 ValueError 由服务层转 Abstain。"""

    payload = _json_object(text)
    kind = payload.get("kind")

    if kind == "abstain" or (
        kind is None and "reason_code" in payload and "abstain" not in payload
    ):
        raw = payload.get("abstain", payload)
        if not isinstance(raw, dict):
            raise ValueError("abstain 字段必须是 JSON 对象")
        return Abstain.model_validate(raw)

    if kind == "insight":
        raw = payload.get("insight")
        if not isinstance(raw, dict):
            raise ValueError("insight 字段必须是 JSON 对象")
        return SetupInsight.model_validate(raw)

    if kind is None and "direction" in payload:
        return SetupInsight.model_validate(payload)

    raise ValueError("宿主输出缺少 kind=insight 或 kind=abstain")
