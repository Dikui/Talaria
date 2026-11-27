"""结构检测器协议、可插拔注册表与集合计算入口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from talaria.foundation.structures.context import DetectionContext
from talaria.foundation.structures.contracts import (
    STRUCTURE_METHOD_VERSION,
    StructureFamily,
    StructureFeature,
    StructureSet,
)


class StructureDetector(Protocol):
    """L0 纯函数结构检测插件协议。"""

    @property
    def detector_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def families(self) -> tuple[StructureFamily, ...]: ...

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]: ...


class PostProcessor(Protocol):
    """L0 纯函数后处理器协议（生命周期、去重、共振聚合、预算裁剪）。"""

    @property
    def processor_id(self) -> str: ...

    def process(
        self, ctx: DetectionContext, features: list[StructureFeature]
    ) -> list[StructureFeature]: ...


@dataclass(slots=True)
class DetectorRegistry:
    """可插拔市场结构检测器与后处理器注册表。"""

    _detectors: dict[str, StructureDetector] = field(default_factory=dict)
    _detector_order: list[str] = field(default_factory=list)
    _postprocessors: dict[str, PostProcessor] = field(default_factory=dict)
    _postprocessor_order: list[str] = field(default_factory=list)

    def register_detector(self, detector: StructureDetector) -> None:
        did = detector.detector_id
        if did in self._detectors:
            raise ValueError(f"重复注册结构检测器：{did}")
        self._detectors[did] = detector
        self._detector_order.append(did)

    def register_postprocessor(self, processor: PostProcessor) -> None:
        pid = processor.processor_id
        if pid in self._postprocessors:
            raise ValueError(f"重复注册后处理器：{pid}")
        self._postprocessors[pid] = processor
        self._postprocessor_order.append(pid)

    def list_detectors(self) -> tuple[str, ...]:
        return tuple(self._detector_order)

    def list_postprocessors(self) -> tuple[str, ...]:
        return tuple(self._postprocessor_order)

    def compute_structures(
        self,
        ctx: DetectionContext,
        *,
        snapshot_id: str = "",
        as_of_ts: int | None = None,
        detector_ids: Sequence[str] | None = None,
        enable_postprocessors: bool = True,
        notes: Sequence[str] = (),
    ) -> StructureSet:
        """运行选定检测器与后处理管线，产出不可变 ``StructureSet``。"""

        if not ctx.visible or ctx.analysis_window is None:
            return StructureSet(
                snapshot_id=snapshot_id,
                instrument_id=ctx.instrument_id,
                timeframe=ctx.timeframe,
                cutoff_ts=ctx.cutoff_ts,
                as_of_ts=ctx.cutoff_ts if as_of_ts is None else as_of_ts,
                method_version=STRUCTURE_METHOD_VERSION,
                detector_ids=[],
                parameters=ctx.params.as_parameters(),
                notes=list(notes),
                structures=[],
            )

        selected = (
            list(self._detector_order)
            if detector_ids is None
            else [did for did in detector_ids]
        )
        for did in selected:
            if did not in self._detectors:
                raise KeyError(f"未注册的结构检测器：{did}")

        collected: list[StructureFeature] = []
        extra_notes = list(notes)
        for did in selected:
            detector = self._detectors[did]
            for item in detector.detect(ctx):
                # 硬防御：任何检测器都不得泄露 cutoff_ts 之后的结构
                if item.ts_available <= ctx.cutoff_ts:
                    collected.append(item)

        # 按 structure_id 去重
        deduped: dict[str, StructureFeature] = {}
        for item in collected:
            deduped.setdefault(item.structure_id, item)
        features = list(deduped.values())

        if enable_postprocessors:
            for pid in self._postprocessor_order:
                features = self._postprocessors[pid].process(ctx, features)

        ordered = sorted(
            features,
            key=lambda item: (-item.strength, -item.ts_available, item.structure_id),
        )
        return StructureSet(
            snapshot_id=snapshot_id,
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            cutoff_ts=ctx.cutoff_ts,
            as_of_ts=ctx.cutoff_ts if as_of_ts is None else as_of_ts,
            method_version=STRUCTURE_METHOD_VERSION,
            detector_ids=selected,
            parameters=ctx.params.as_parameters(),
            notes=extra_notes,
            structures=ordered,
        )
