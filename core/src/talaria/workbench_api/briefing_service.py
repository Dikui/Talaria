"""扫描命中后的协同简报服务。

设计要点：

* ``offer`` 是同步的、非阻塞的门禁：冷却 → 令牌桶 → 全局并发，任何一项
  不通过都立刻返回 ``False``，由 ``TriggerRoutingPolicy`` 把触发降级为
  LOCAL 纯数值卡，不建简报、不调用模型、不排队。
* 通过后才异步派发一次宿主请求；服务端从不按时间轮询模型。
* 简报始终绑定触发时的 ``snapshot_id``，模型输出只能引用该快照的证据目录。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from talaria.foundation.briefings import (
    BriefingConsultResponse,
    BriefingContent,
    BriefingResultRequest,
    BriefingStatus,
    ScannerBriefing,
    build_briefing_prompt,
    parse_briefing_text,
)
from talaria.foundation.collaboration import EvidenceKind, EvidenceRef
from talaria.foundation.errors import InvalidRequestError
from talaria.foundation.scanner import ScannerTrigger
from talaria.workbench_api.state_store import StateWriterQueue
from talaria.workbench_api.usage_service import UsageService

logger = logging.getLogger(__name__)

HostDispatcher = Callable[[str, Mapping[str, Any]], Awaitable[None]]
BRIEFING_PATH = "/talaria/briefing"
DEFAULT_COOLDOWN_MS = 300_000
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_RATE_PER_MINUTE = 6.0
DEFAULT_OVERALL_TIMEOUT_S = 90.0
DEFAULT_HTTP_TIMEOUT_S = 10.0
MAX_EVIDENCE = 40


class _TokenBucket:
    """惰性补充的令牌桶；``take`` 永不等待。"""

    def __init__(self, *, capacity: int, rate_per_minute: float, now_ms: int) -> None:
        self._capacity = float(max(1, capacity))
        self._tokens = self._capacity
        self._rate_per_ms = max(0.0, rate_per_minute) / 60_000.0
        self._updated_ms = now_ms

    def take(self, now_ms: int) -> bool:
        elapsed = max(0, now_ms - self._updated_ms)
        self._updated_ms = max(self._updated_ms, now_ms)
        self._tokens = min(
            self._capacity, self._tokens + elapsed * self._rate_per_ms
        )
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class BriefingService:
    """实现 15 预留的 ``CoPilotConsultChannel``，并管理简报持久化。"""

    def __init__(
        self,
        *,
        queue: StateWriterQueue,
        usage: UsageService,
        workbench: Any,
        host_url: Callable[[], str | None],
        now_ms: Callable[[], int],
        publish: Callable[[ScannerBriefing], None] | None = None,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        rate_per_minute: float = DEFAULT_RATE_PER_MINUTE,
        overall_timeout_s: float = DEFAULT_OVERALL_TIMEOUT_S,
        dispatcher: HostDispatcher | None = None,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
    ) -> None:
        self._queue = queue
        self._usage = usage
        self._workbench = workbench
        self._host_url = host_url
        self._now_ms = now_ms
        self._publish_cb = publish
        self._cooldown_ms = max(0, cooldown_ms)
        self._max_concurrency = max(1, max_concurrency)
        self._overall_timeout_s = overall_timeout_s
        self._http_timeout_s = http_timeout_s
        self._dispatcher = dispatcher or self._post_to_host
        self._bucket = _TokenBucket(
            capacity=max(1, int(rate_per_minute // 60)),
            rate_per_minute=rate_per_minute,
            now_ms=now_ms(),
        )
        self._cooldown_until: dict[str, int] = {}
        self._inflight = 0
        self._events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- 门禁

    @property
    def pending(self) -> bool:
        return bool(self._tasks) or self._inflight > 0

    def _host(self) -> str | None:
        try:
            value = self._host_url()
        except Exception:  # pragma: no cover - 宿主探测异常按离线处理
            logger.exception("协同简报宿主地址读取失败，按离线降级")
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def offer(self, trigger: ScannerTrigger) -> bool:
        """同步门禁；通过则启动一个后台任务并立即返回 ``True``。"""

        if not trigger.snapshot_id:
            return False
        if self._host() is None:
            return False

        now = self._now_ms()
        cooldown_until = self._cooldown_until.get(trigger.instrument_id, 0)
        if now < cooldown_until:
            return False
        if self._inflight >= self._max_concurrency:
            return False
        if not self._bucket.take(now):
            return False

        self._cooldown_until[trigger.instrument_id] = now + self._cooldown_ms
        self._inflight += 1
        task = asyncio.create_task(self._run(trigger))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    # -------------------------------------------------------------- 派发

    def _evidence(self, trigger: ScannerTrigger) -> list[EvidenceRef]:
        refs: list[EvidenceRef] = [
            EvidenceRef(
                evidence_id=f"snapshot:{trigger.snapshot_id}",
                kind=EvidenceKind.SNAPSHOT,
                label=f"冻结快照 {trigger.snapshot_id}",
                detail=(
                    f"{trigger.instrument_id} · {trigger.timeframe} · "
                    f"触发 {trigger.created_ts}"
                ),
            )
        ]
        for index, item in enumerate(trigger.evidence):
            refs.append(
                EvidenceRef(
                    evidence_id=f"scanner:{trigger.trigger_id}:{index}",
                    kind=EvidenceKind.INDICATOR,
                    label=item.metric,
                    detail=(
                        f"{item.value}"
                        + (f" / 阈值 {item.threshold}" if item.threshold else "")
                        + (f" · {item.note}" if item.note else "")
                    )[:500],
                )
            )
        if self._workbench is not None:
            try:
                level_set = self._workbench.get_key_levels(
                    trigger.snapshot_id,
                    timeframe=trigger.timeframe,
                    top_n=12,
                )
            except Exception:  # pragma: no cover - 关键位失败不阻塞简报
                logger.exception("简报读取关键位失败，跳过 level 证据")
            else:
                for level in getattr(level_set, "levels", []):
                    refs.append(
                        EvidenceRef(
                            evidence_id=f"level:{level.level_id}",
                            kind=EvidenceKind.KEY_LEVEL,
                            label=f"关键位 {level.level_id}",
                            detail=(
                                f"{getattr(level, 'kind', '')} · "
                                f"{getattr(level, 'strength', '')}"
                            )[:500],
                            price_low=getattr(level, "price_low", None)
                            or getattr(level, "price", None),
                            price_high=getattr(level, "price_high", None)
                            or getattr(level, "price", None),
                        )
                    )
        return refs[:MAX_EVIDENCE]

    def _catalog_evidence(
        self, record: ScannerBriefing, refs: list[EvidenceRef]
    ) -> list[EvidenceRef]:
        """Abstain 也可以带证据，但只能引用已冻结的证据目录。"""

        known = {item.evidence_id: item for item in record.evidence_refs}
        selected: list[EvidenceRef] = []
        seen: set[str] = set()
        for item in refs:
            hit = known.get(item.evidence_id)
            if hit is not None and hit.evidence_id not in seen:
                selected.append(hit)
                seen.add(hit.evidence_id)
        return selected or record.evidence_refs[:MAX_EVIDENCE]

    def _publish(self, record: ScannerBriefing) -> None:
        if self._publish_cb is None:
            return
        try:
            self._publish_cb(record)
        except Exception:  # pragma: no cover - 广播失败不影响落库
            logger.exception("简报广播失败")

    async def _run(self, trigger: ScannerTrigger) -> None:
        try:
            record = ScannerBriefing(
                id=f"brf_{uuid.uuid4().hex}",
                trigger_id=trigger.trigger_id,
                snapshot_id=trigger.snapshot_id,
                instrument_id=trigger.instrument_id,
                timeframe=trigger.timeframe,
                status=BriefingStatus.PENDING,
                evidence_refs=self._evidence(trigger),
                created_ts=trigger.created_ts or self._now_ms(),
            )
            await self._queue.run(lambda store: store.briefings.create(record))
            self._publish(record)

            event = asyncio.Event()
            self._events[record.id] = event

            url = self._host()
            if url is None:
                await self._finish_abstain(record, "HOST_OFFLINE", "协同宿主离线")
                return

            prompt = build_briefing_prompt(
                briefing=record, trigger=trigger, evidence=record.evidence_refs
            )
            payload = {
                "briefing_id": record.id,
                "trigger_id": trigger.trigger_id,
                "snapshot_id": trigger.snapshot_id,
                "prompt": prompt,
                "timeout_ms": int(self._overall_timeout_s * 1000),
            }
            try:
                await self._dispatcher(url, payload)
            except Exception:
                logger.exception("协同简报派发失败，降级为 Abstain")
                await self._finish_abstain(
                    record, "HOST_OFFLINE", "协同宿主连接失败"
                )
                return

            current = await self._queue.read(
                lambda store: store.briefings.get(record.id)
            )
            if current.status is not BriefingStatus.PENDING:
                return
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=self._overall_timeout_s
                )
            except TimeoutError:
                await self._finish_abstain(record, "MODEL_TIMEOUT", "简报生成超时")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 兜底，避免后台任务静默死亡
            logger.exception("协同简报任务异常")
        finally:
            self._inflight = max(0, self._inflight - 1)

    async def _finish_abstain(
        self, record: ScannerBriefing, reason: str, message: str
    ) -> None:
        current = await self._queue.run(lambda store: store.briefings.get(record.id))
        if current.status is not BriefingStatus.PENDING:
            return
        updated = current.model_copy(
            update={
                "status": BriefingStatus.ABSTAIN,
                "abstain_reason": reason,
                "summary": None,
            }
        )
        await self._queue.run(lambda store: store.briefings.update(updated))
        self._publish(updated)
        event = self._events.get(record.id)
        if event is not None:
            event.set()
        logger.info("协同简报 %s 降级为 Abstain：%s", record.id, message)

    async def _post_to_host(
        self, url: str, payload: Mapping[str, Any]
    ) -> None:
        import httpx

        async with httpx.AsyncClient(
            timeout=self._http_timeout_s, trust_env=False
        ) as client:
            response = await client.post(
                f"{url.rstrip('/')}{BRIEFING_PATH}", json=dict(payload)
            )
            response.raise_for_status()

    # -------------------------------------------------------------- 结果

    async def submit_result(
        self, briefing_id: str, payload: BriefingResultRequest
    ) -> ScannerBriefing:
        record = await self._queue.run(lambda store: store.briefings.get(briefing_id))
        if payload.usage is not None:
            await self._usage.record(payload.usage, now_ms=self._now_ms)
        if record.status is not BriefingStatus.PENDING:
            return record

        if payload.error is not None:
            updated = record.model_copy(update={"status": BriefingStatus.FAILED})
            return await self._commit(updated)

        if payload.abstain is not None:
            updated = record.model_copy(
                update={
                    "status": BriefingStatus.ABSTAIN,
                    "summary": None,
                    "abstain_reason": payload.abstain.reason_code.value,
                    "evidence_refs": self._catalog_evidence(
                        record, payload.abstain.evidence_refs
                    ),
                }
            )
            return await self._commit(updated)

        content: BriefingContent | None
        evidence_ids: list[str] | None = None
        if payload.content is not None:
            content = payload.content
        else:
            try:
                content, evidence_ids, parsed_abstain = parse_briefing_text(
                    payload.text or ""
                )
            except ValueError:
                logger.warning("简报输出不是合法结构，降级为 Abstain", exc_info=True)
                updated = record.model_copy(
                    update={
                        "status": BriefingStatus.ABSTAIN,
                        "abstain_reason": "MODEL_INVALID_OUTPUT",
                        "summary": None,
                    }
                )
                return await self._commit(updated)
            if parsed_abstain is not None:
                updated = record.model_copy(
                    update={
                        "status": BriefingStatus.ABSTAIN,
                        "summary": None,
                        "abstain_reason": parsed_abstain.reason_code.value,
                        "evidence_refs": self._catalog_evidence(
                            record, parsed_abstain.evidence_refs
                        ),
                    }
                )
                return await self._commit(updated)

        if content is None:  # pragma: no cover - parse_briefing_text 已保证二选一
            raise ValueError("简报结果缺少正文")
        known = {ref.evidence_id: ref for ref in record.evidence_refs}
        if evidence_ids:
            unknown = [item for item in evidence_ids if item not in known]
            if unknown:
                updated = record.model_copy(
                    update={
                        "status": BriefingStatus.ABSTAIN,
                        "abstain_reason": "EVIDENCE_CONFLICT",
                        "summary": None,
                    }
                )
                return await self._commit(updated)
            refs = [known[item] for item in evidence_ids]
        else:
            refs = record.evidence_refs[:MAX_EVIDENCE]

        updated = record.model_copy(
            update={
                "status": BriefingStatus.READY,
                "summary": content,
                "evidence_refs": refs,
                "abstain_reason": None,
            }
        )
        return await self._commit(updated)

    async def _commit(self, record: ScannerBriefing) -> ScannerBriefing:
        await self._queue.run(lambda store: store.briefings.update(record))
        self._publish(record)
        event = self._events.get(record.id)
        if event is not None:
            event.set()
        return record

    # -------------------------------------------------------------- 读取

    async def list(
        self, *, limit: int = 50, unread_only: bool = False
    ) -> list[ScannerBriefing]:
        return await self._queue.read(
            lambda store: store.briefings.list(
                limit=limit, unread_only=unread_only
            )
        )

    async def get(self, briefing_id: str) -> ScannerBriefing:
        return await self._queue.read(
            lambda store: store.briefings.get(briefing_id)
        )

    async def unread_count(self) -> int:
        return await self._queue.read(
            lambda store: store.briefings.count_unread()
        )

    async def mark_read(self, briefing_id: str) -> ScannerBriefing:
        return await self._queue.run(
            lambda store: store.briefings.mark_read(briefing_id, self._now_ms())
        )

    async def consult_seed(
        self, briefing_id: str, question: str | None = None
    ) -> BriefingConsultResponse:
        record = await self.get(briefing_id)
        if record.status is not BriefingStatus.READY:
            raise InvalidRequestError("简报尚未生成有效观点，不能带入对话")
        seed = question or (
            f"请结合 {record.instrument_id} {record.timeframe} 的冻结快照 "
            f"{record.snapshot_id}，解读这次 {record.trigger_id} 异动："
            "关键价位、主要风险，以及还需要补充哪些证据。"
        )
        return BriefingConsultResponse(
            briefing_id=record.id,
            snapshot_id=record.snapshot_id,
            instrument_id=record.instrument_id,
            timeframe=record.timeframe,
            question=seed[:2000],
        )

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._events.clear()
