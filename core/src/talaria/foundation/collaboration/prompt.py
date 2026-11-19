"""会诊提示词的三段组装。

顺序固定为：静态分析纪律前缀 → 加密永续 Market Pack 领域技能 → 当前快照
增量。第三段只放冻结后的会诊包与只读工具结果摘要，不从任何外部行情源补数。
"""

from __future__ import annotations

import json

from talaria.foundation.collaboration.contracts import (
    ConsultationPackage,
    EvidenceKind,
    EvidenceRef,
)

DISCIPLINE_PREFIX = """# 第一段 · 静态分析纪律

你是 Talaria 的协同分析智能体，服务对象是正在看图的交易员。

硬性纪律：
1. 一切行情、指标、关键位与画线都来自当前冻结快照；禁止引用快照之外的行情，
   禁止使用记忆中的价格，禁止调用任何外部行情或搜索接口补充数据。
2. 只读：禁止写入、修改、删除画线、预警、关键位或任何状态；你不能下单。
3. 证据必须来自工具结果、HUMAN_DRAWING 画线或关键位。每条结论都要在
   ``evidence_refs`` 中给出 ``evidence_id``，且 ID 必须出现在本提示词附带的
   「证据目录」中；不得编造 ID。
4. 指标只调用 ``get_indicators`` / ``compute_operator`` 由 Python 计算，禁止
   在推理中自行重算均线、RSI、ATR 等数值。
5. 证据互相矛盾、止损/目标方向倒挂、快照过期或无法确认时，输出 Abstain，
   不要为了给出结论而猜测。
6. 输出是观察（Setup Insight），不是订单；必须包含入场带、证伪止损、止盈、
   持仓上限、有效期与证据引用。
"""

MARKET_PACK_SKILL = """# 第二段 · 加密永续 Market Pack 领域技能

标的为 USDT 本位永续合约，24/7 连续交易，使用标记价格与资金费率，存在
强平与插针风险。

分析约定：
- 先判断更高周期结构，再用当前周期找入场；入场带应贴着有证据的支撑/阻力，
  止损放在使交易逻辑失效的位置之外，而不是固定百分比。
- 关键位证据类别：STRUCTURE / SESSION / GAP / ROUND_NUMBER / VOLUME /
  LIQUIDATION。低强度或 ESTIMATED 的关键位只能作辅助证据。
- HUMAN_DRAWING 是交易员的主观结构：水平线、射线、线段、通道、斐波那契、
  矩形与文字标注。尊重画线，但不要把它当成自动成交信号。
- 加密永续的周末、资金费率结算前后与低流动性时段会放大假突破；有效期应
  与当前周期匹配，通常不超过若干根 K 线。
- 仓位上限用账户风险比例或合约数量表示，不得给出超出证据支持的建议。
"""

OUTPUT_CONTRACT = """# 输出格式（只输出一个 JSON 对象，不要 Markdown 代码围栏之外的解释）

给出结论：
{
  "kind": "insight",
  "insight": {
    "direction": "LONG" | "SHORT" | "NEUTRAL",
    "summary": "中文结论与触发条件",
    "entry_low": "十进制字符串",
    "entry_high": "十进制字符串",
    "invalidation_stop": "十进制字符串",
    "take_profit": ["十进制字符串"],
    "max_position": "十进制字符串或 null",
    "valid_until": 毫秒时间戳或 null,
    "evidence_refs": [{"evidence_id": "证据目录中的 ID", "kind": "…", "label": "…"}]
  }
}

无法给出可靠结论时：
{
  "kind": "abstain",
  "abstain": {
    "reason_code": "STALE_SNAPSHOT" | "EVIDENCE_CONFLICT" |
                   "GEOMETRY_INVERTED" | "MODEL_TIMEOUT" |
                   "HOST_OFFLINE" | "MODEL_INVALID_OUTPUT",
    "message": "中文原因",
    "evidence_refs": [{"evidence_id": "…", "kind": "…", "label": "…"}]
  }
}

价格一律用十进制字符串，禁止科学计数法或数字类型。证据不足时必须 Abstain。
"""

_EVIDENCE_LABELS = {
    EvidenceKind.SNAPSHOT: "快照",
    EvidenceKind.CHART_VIEW: "视窗",
    EvidenceKind.KEY_LEVEL: "关键位",
    EvidenceKind.INDICATOR: "指标",
    EvidenceKind.OPERATOR: "算子",
    EvidenceKind.HUMAN_DRAWING: "人工画线",
}


def _evidence_line(ref: EvidenceRef) -> str:
    label = _EVIDENCE_LABELS.get(ref.kind, ref.kind.value)
    price = ""
    if ref.price_low is not None and ref.price_high is not None:
        price = f" · 价格 {ref.price_low}–{ref.price_high}"
    detail = f" · {ref.detail}" if ref.detail else ""
    return f"- [{label}] `{ref.evidence_id}` {ref.label}{price}{detail}"


def _evidence_catalog(package: ConsultationPackage) -> str:
    if not package.evidence_catalog:
        return "（无；没有证据时必须 Abstain）"
    grouped: dict[EvidenceKind, list[EvidenceRef]] = {}
    for ref in package.evidence_catalog:
        grouped.setdefault(ref.kind, []).append(ref)
    lines: list[str] = []
    for kind in EvidenceKind:
        refs = grouped.get(kind)
        if refs:
            lines.append(f"### {_EVIDENCE_LABELS[kind]}")
            lines.extend(_evidence_line(ref) for ref in refs)
    return "\n".join(lines)


def _tool_summary(package: ConsultationPackage) -> str:
    """把证据目录压成宿主可直接读的只读工具结果摘要。"""

    summary = {
        "snapshot_id": package.snapshot_id,
        "visible": package.visible.model_dump(mode="json"),
        "selection": (
            package.selection.model_dump(mode="json")
            if package.selection is not None
            else None
        ),
        "drawing_count": len(package.drawings),
        "catalyst_count": len(package.catalysts),
        "evidence_count": len(package.evidence_catalog),
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def build_consultation_prompt(package: ConsultationPackage) -> str:
    """三段式提示词；只使用冻结包，函数本身不访问任何外部状态。"""

    package_json = json.dumps(
        package.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    snapshot_section = f"""# 第三段 · 当前快照增量

本轮的 ``snapshot_id`` 是 `{package.snapshot_id}`，所有工具调用必须绑定它。
快照截止时间：{package.snapshot_cutoff_ts}（UTC 毫秒）。
标的：`{package.instrument_id}`；周期：`{package.timeframe}`。

## 交易员的问题
{package.question}

## 会诊包（冻结）
```json
{package_json}
```

## 只读工具结果摘要
```json
{_tool_summary(package)}
```

## 证据目录（只允许引用这些 ID）
{_evidence_catalog(package)}

## 几何约束
- LONG：止损 < 入场带下沿，止盈 > 入场带上沿。
- SHORT：止损 > 入场带上沿，止盈 < 入场带下沿。
- 若给了框选区间，入场带必须与框选价格区间相交。
- 引用带价格的关键位/画线时，其价格区间必须与「止损到最远止盈」的交易带相交。

{OUTPUT_CONTRACT}"""

    return "\n\n".join(
        [DISCIPLINE_PREFIX.strip(), MARKET_PACK_SKILL.strip(), snapshot_section.strip()]
    )
