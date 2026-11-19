"""简报提示词：只读工具 + 冻结快照 + 四段 JSON 输出。

提示词不包含任何写操作或下单指令；宿主离线或输出不合规时服务端会降级为
Abstain，而不是让模型自由发挥。
"""

from __future__ import annotations

import json
import re
from typing import Any

from talaria.foundation.briefings.contracts import (
    BriefingContent,
    BriefingLevelRef,
    ScannerBriefing,
)
from talaria.foundation.collaboration import Abstain, AbstainReason, EvidenceRef
from talaria.foundation.scanner import ScannerTrigger

_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

BRIEFING_DISCIPLINE = (
    "你是 Talaria 的协同简报员。只允许使用本轮提供的只读工具读取冻结快照；"
    "禁止交易、撤单、改参数或写入任何状态。所有结论必须能回指证据目录中的 ID。"
)


def _catalog_lines(refs: list[EvidenceRef]) -> str:
    lines: list[str] = []
    for ref in refs:
        price = ""
        if ref.price_low is not None and ref.price_high is not None:
            price = f" · 价格 {ref.price_low}–{ref.price_high}"
        detail = f" · {ref.detail}" if ref.detail else ""
        lines.append(f"- {ref.evidence_id} | {ref.kind.value} | {ref.label}{price}{detail}")
    return "\n".join(lines)


def build_briefing_prompt(
    *,
    briefing: ScannerBriefing,
    trigger: ScannerTrigger,
    evidence: list[EvidenceRef],
) -> str:
    """构造一段绑定 ``snapshot_id`` 的简报提示词。"""

    evidence_json = _catalog_lines(evidence)
    return (
        f"{BRIEFING_DISCIPLINE}\n\n"
        "## 数值触发\n"
        f"- 简报 ID：{briefing.id}\n"
        f"- 触发 ID：{trigger.trigger_id}\n"
        f"- 标的：{trigger.instrument_id}\n"
        f"- 周期：{trigger.timeframe}\n"
        f"- 类型：{trigger.kind.value}\n"
        f"- 严重度：{trigger.severity.value}\n"
        f"- 标题：{trigger.title}\n"
        f"- 摘要：{trigger.summary}\n"
        f"- 快照：{trigger.snapshot_id}\n\n"
        "## 冻结快照与证据目录\n"
        "只可读取以下 snapshot_id，不得引用其他快照：\n"
        f"{trigger.snapshot_id}\n\n"
        f"{evidence_json}\n\n"
        "## 输出要求\n"
        "只输出一个 JSON 对象，不要 Markdown 代码围栏之外的文字。格式：\n"
        "{\n"
        '  "trigger_reason": "一句话说明为什么会触发，以及当前结构含义",\n'
        '  "key_levels": [{"label": "前高", "price": "123.45", "note": "观察承接"}],\n'
        '  "risks": ["若跌回 ... 则 ..."],\n'
        '  "evidence_ids": ["snapshot:...", "scanner:..."]\n'
        "}\n"
        "key_levels 最多 8 条，risks 最多 6 条；price 必须是十进制字符串，"
        "未知时省略该字段。evidence_ids 只能从上面的证据目录选择，至少一条。\n"
        "如果没有足够证据形成可执行观察，输出 "
        '{"abstain": {"reason_code": "EVIDENCE_CONFLICT", "message": "原因", '
        '"evidence_refs": ["snapshot:..."]}}；evidence_refs 可省略，'
        "若填写则只能来自上面的证据目录。\n"
    )


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = _CODE_FENCE.sub("", text.strip()).strip()
    if cleaned.startswith("{"):
        end = cleaned.rfind("}")
        if end >= 0:
            cleaned = cleaned[: end + 1]
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("简报输出必须是 JSON 对象")
    return payload


def parse_briefing_text(
    text: str,
) -> tuple[BriefingContent | None, list[str], Abstain | None]:
    """解析宿主文本为正文/证据 ID，或显式 Abstain；不合法时抛 ``ValueError``。"""

    payload = _extract_json(text)
    if "abstain" in payload:
        raw = payload["abstain"]
        if not isinstance(raw, dict):
            raise ValueError("abstain 必须是对象")
        reason_code = raw.get("reason_code")
        try:
            code = AbstainReason(reason_code)
        except (TypeError, ValueError) as exc:
            raise ValueError("abstain.reason_code 不合法") from exc
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("abstain.message 不能为空")
        raw_refs = raw.get("evidence_refs", [])
        if not isinstance(raw_refs, list):
            raise ValueError("abstain.evidence_refs 必须是数组")
        refs = [EvidenceRef.model_validate(item) for item in raw_refs[:40]]
        return None, [], Abstain(
            reason_code=code,
            message=message.strip()[:1000],
            evidence_refs=refs,
        )
    reason = payload.get("trigger_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("缺少 trigger_reason")
    raw_levels = payload.get("key_levels", [])
    if not isinstance(raw_levels, list):
        raise ValueError("key_levels 必须是数组")
    levels: list[BriefingLevelRef] = []
    for item in raw_levels[:12]:
        if not isinstance(item, dict):
            raise ValueError("key_levels 的每一项必须是对象")
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("key_levels 缺少 label")
        price = item.get("price")
        if price is not None and not isinstance(price, (str, int, float)):
            raise ValueError("key_levels.price 必须是十进制字符串")
        note = item.get("note", "")
        if not isinstance(note, str):
            raise ValueError("key_levels.note 必须是字符串")
        levels.append(
            BriefingLevelRef(
                label=label.strip(),
                price=str(price) if price is not None else None,
                note=note,
            )
        )
    raw_risks = payload.get("risks", [])
    if not isinstance(raw_risks, list) or any(
        not isinstance(item, str) for item in raw_risks
    ):
        raise ValueError("risks 必须是字符串数组")
    raw_ids = payload.get("evidence_ids", [])
    if not isinstance(raw_ids, list) or any(
        not isinstance(item, str) for item in raw_ids
    ):
        raise ValueError("evidence_ids 必须是字符串数组")
    content = BriefingContent(
        trigger_reason=reason.strip(),
        key_levels=levels,
        risks=[item.strip() for item in raw_risks if item.strip()][:12],
    )
    return content, [item for item in raw_ids if item], None
