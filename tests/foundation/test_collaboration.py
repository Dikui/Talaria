"""任务单 17：协同会诊的提示词、Abstain 语义与会诊包契约。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.foundation.collaboration import (
    Abstain,
    AbstainReason,
    CollabWindow,
    ConsultationPackage,
    ConsultRequest,
    DrawingRef,
    EvidenceKind,
    EvidenceRef,
    SetupDirection,
    SetupInsight,
    build_consultation_prompt,
    evaluate_insight,
    parse_host_analysis,
    stale_abstain,
)

pytestmark = pytest.mark.small

NOW_MS = 1_795_022_974_103
FRESH_WINDOW = 15 * 60 * 1000


def _window(*, price_min: str = "60000", price_max: str = "70000") -> CollabWindow:
    return CollabWindow(
        from_ts=NOW_MS - 3_600_000,
        to_ts=NOW_MS,
        price_min=Decimal(price_min),
        price_max=Decimal(price_max),
    )


def _evidence_catalog() -> list[EvidenceRef]:
    return [
        EvidenceRef(
            evidence_id="snapshot:snap_1",
            kind=EvidenceKind.SNAPSHOT,
            label="快照 snap_1",
            detail="BTC-USDT-PERP 1m",
        ),
        EvidenceRef(
            evidence_id="chart_view:BTC-USDT-PERP:1m",
            kind=EvidenceKind.CHART_VIEW,
            label="视窗 1m",
        ),
        EvidenceRef(
            evidence_id="level:lvl_support",
            kind=EvidenceKind.KEY_LEVEL,
            label="前日低点",
            price_low=Decimal("62000"),
            price_high=Decimal("62100"),
        ),
        EvidenceRef(
            evidence_id="drawing:drw_box",
            kind=EvidenceKind.HUMAN_DRAWING,
            label="需求区",
            price_low=Decimal("61800"),
            price_high=Decimal("62200"),
        ),
        EvidenceRef(
            evidence_id="indicator:EMA",
            kind=EvidenceKind.INDICATOR,
            label="EMA 20/60",
        ),
    ]


def _package(**overrides) -> ConsultationPackage:
    base = dict(
        consultation_id="cns_1",
        snapshot_id="snap_1",
        instrument_id="BTC-USDT-PERP",
        timeframe="1m",
        visible=_window(),
        selection=None,
        drawings=[
            DrawingRef(
                drawing_id="drw_box",
                kind="RECTANGLE",
                points=[
                    {"ts": NOW_MS - 600_000, "price": "61800"},
                    {"ts": NOW_MS - 540_000, "price": "62200"},
                ],
                text="需求区",
            )
        ],
        catalysts=[],
        question="这里能接多吗？",
        snapshot_cutoff_ts=NOW_MS - 60_000,
        evidence_catalog=_evidence_catalog(),
        created_ts=NOW_MS - 30_000,
    )
    base.update(overrides)
    return ConsultationPackage(**base)


def _insight(**overrides) -> SetupInsight:
    base = dict(
        direction=SetupDirection.LONG,
        summary="回踩需求区接多，跌回区间下沿证伪。",
        entry_low=Decimal("62000"),
        entry_high=Decimal("62150"),
        invalidation_stop=Decimal("61600"),
        take_profit=[Decimal("63200"), Decimal("64100")],
        max_position=Decimal("0.25"),
        valid_until=NOW_MS + 3_600_000,
        evidence_refs=[
            EvidenceRef(
                evidence_id="snapshot:snap_1",
                kind=EvidenceKind.SNAPSHOT,
                label="快照 snap_1",
            ),
            EvidenceRef(
                evidence_id="level:lvl_support",
                kind=EvidenceKind.KEY_LEVEL,
                label="前日低点",
                price_low=Decimal("62000"),
                price_high=Decimal("62100"),
            ),
        ],
    )
    base.update(overrides)
    return SetupInsight(**base)


def test_prompt_has_three_sections_and_forbids_outside_quotes() -> None:
    prompt = build_consultation_prompt(_package())

    assert prompt.index("静态分析纪律") < prompt.index("加密永续 Market Pack")
    assert prompt.index("加密永续 Market Pack") < prompt.index("当前快照增量")
    assert "snap_1" in prompt
    assert "禁止引用快照之外的行情" in prompt
    assert "禁止写入" in prompt
    assert "证据必须来自" in prompt
    assert "请求协同会诊" not in prompt


def test_package_serializes_decimal_as_strings() -> None:
    payload = _package().model_dump(mode="json")

    assert payload["visible"]["price_min"] == "60000"
    assert payload["drawings"][0]["points"][0]["price"] == "61800"
    assert isinstance(payload["drawings"][0]["points"][0]["price"], str)


def test_consult_request_keeps_snapshot_and_question() -> None:
    request = ConsultRequest(
        snapshot_id="snap_1",
        instrument_id="BTC-USDT-PERP",
        timeframe="1m",
        question="  这只票能接多吗？  ",
        visible=_window(),
        selection=None,
        drawing_ids=["drw_box"],
    )

    assert request.question == "这只票能接多吗？"
    assert request.snapshot_id == "snap_1"


def test_stale_snapshot_returns_reason_and_evidence() -> None:
    package = _package()
    abstain = stale_abstain(package, now_ms=NOW_MS + FRESH_WINDOW + 1, stale_after_ms=FRESH_WINDOW)

    assert abstain is not None
    assert abstain.reason_code is AbstainReason.STALE_SNAPSHOT
    assert abstain.evidence_refs[0].evidence_id == "snapshot:snap_1"
    assert "STALE" in abstain.message


def test_fresh_snapshot_is_not_stale() -> None:
    assert stale_abstain(_package(), now_ms=NOW_MS, stale_after_ms=FRESH_WINDOW) is None


def test_long_geometry_inverted_when_stop_above_entry() -> None:
    abstain = evaluate_insight(_package(), _insight(invalidation_stop=Decimal("62500")))

    assert abstain is not None
    assert abstain.reason_code is AbstainReason.GEOMETRY_INVERTED
    assert "止损" in abstain.message


def test_long_geometry_inverted_when_target_below_entry() -> None:
    abstain = evaluate_insight(
        _package(),
        _insight(take_profit=[Decimal("61000"), Decimal("64100")]),
    )

    assert abstain is not None
    assert abstain.reason_code is AbstainReason.GEOMETRY_INVERTED


def test_short_geometry_must_mirror_long() -> None:
    short = _insight(
        direction=SetupDirection.SHORT,
        summary="反抽上方阻力做空。",
        entry_low=Decimal("69800"),
        entry_high=Decimal("69950"),
        invalidation_stop=Decimal("70400"),
        take_profit=[Decimal("68800"), Decimal("68100")],
        evidence_refs=[
            EvidenceRef(
                evidence_id="level:lvl_support",
                kind=EvidenceKind.KEY_LEVEL,
                label="前日低点",
                price_low=Decimal("62000"),
                price_high=Decimal("62100"),
            )
        ],
    )

    abstain = evaluate_insight(_package(), short)
    assert abstain is not None
    assert abstain.reason_code is AbstainReason.EVIDENCE_CONFLICT


def test_unknown_evidence_reference_is_a_conflict() -> None:
    insight = _insight(
        evidence_refs=[
            EvidenceRef(
                evidence_id="level:lvl_forged",
                kind=EvidenceKind.KEY_LEVEL,
                label="不存在",
            )
        ]
    )

    abstain = evaluate_insight(_package(), insight)
    assert abstain is not None
    assert abstain.reason_code is AbstainReason.EVIDENCE_CONFLICT
    assert "lvl_forged" in abstain.message
    assert "level:lvl_forged" in abstain.evidence_refs[0].evidence_id


def test_no_evidence_is_a_conflict() -> None:
    abstain = evaluate_insight(_package(), _insight(evidence_refs=[]))

    assert abstain is not None
    assert abstain.reason_code is AbstainReason.EVIDENCE_CONFLICT


def test_entry_band_disjoint_from_selection_is_a_conflict() -> None:
    package = _package(
        selection=CollabWindow(
            from_ts=NOW_MS - 300_000,
            to_ts=NOW_MS - 200_000,
            price_min=Decimal("69000"),
            price_max=Decimal("69500"),
        )
    )

    abstain = evaluate_insight(package, _insight())
    assert abstain is not None
    assert abstain.reason_code is AbstainReason.EVIDENCE_CONFLICT


def test_cited_level_far_outside_trade_band_is_a_conflict() -> None:
    package = _package(
        evidence_catalog=[
            *_evidence_catalog(),
            EvidenceRef(
                evidence_id="level:lvl_far",
                kind=EvidenceKind.KEY_LEVEL,
                label="远处关键位",
                price_low=Decimal("50000"),
                price_high=Decimal("50100"),
            ),
        ]
    )
    insight = _insight(
        evidence_refs=[
            EvidenceRef(
                evidence_id="level:lvl_far",
                kind=EvidenceKind.KEY_LEVEL,
                label="远处关键位",
                price_low=Decimal("50000"),
                price_high=Decimal("50100"),
            )
        ]
    )

    abstain = evaluate_insight(package, insight)
    assert abstain is not None
    assert abstain.reason_code is AbstainReason.EVIDENCE_CONFLICT


def test_valid_insight_passes() -> None:
    assert evaluate_insight(_package(), _insight()) is None


def test_parse_host_analysis_accepts_fenced_json() -> None:
    text = """先给结论：\n```json\n{"kind": "insight", "insight": %s}\n```""" % _insight().model_dump_json()
    insight = parse_host_analysis(text)

    assert isinstance(insight, SetupInsight)
    assert insight.direction is SetupDirection.LONG


def test_parse_host_analysis_accepts_abstain() -> None:
    text = (
        '{"kind": "abstain", "abstain": {"reason_code": "EVIDENCE_CONFLICT", '
        '"message": "关键位与画线矛盾", "evidence_refs": []}}'
    )
    abstain = parse_host_analysis(text)

    assert isinstance(abstain, Abstain)
    assert abstain.reason_code is AbstainReason.EVIDENCE_CONFLICT


def test_parse_host_analysis_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_host_analysis("我觉得可以接多。")
