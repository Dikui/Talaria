"""关键位忽略反馈：按 instrument + level_id 持久化到协同状态库。

所有写入复用 ``StateWriterQueue`` 的单写协程；冻结快照与 ``drawings`` 表都不参与。
"""

from __future__ import annotations

from talaria.foundation.errors import InvalidRequestError
from talaria.foundation.key_levels import (
    KeyLevelIgnoreList,
    KeyLevelIgnoreRecord,
    KeyLevelIgnoreRequest,
)
from talaria.workbench_api.state_store import StateWriterQueue


class KeyLevelFeedbackService:
    def __init__(self, *, state_writer: StateWriterQueue) -> None:
        self._state_writer = state_writer

    async def list(self, instrument_id: str) -> KeyLevelIgnoreList:
        rows = await self._state_writer.run(
            lambda store: store.list_key_level_ignores(instrument_id)
        )
        return KeyLevelIgnoreList(
            instrument_id=instrument_id,
            entries=[KeyLevelIgnoreRecord(**row) for row in rows],
        )

    async def set_ignored(
        self, request: KeyLevelIgnoreRequest
    ) -> KeyLevelIgnoreList:
        if not request.instrument_id or not request.level_id:
            raise InvalidRequestError("instrument_id 与 level_id 不能为空")
        await self._state_writer.run(
            lambda store: store.set_key_level_ignore(
                request.instrument_id, request.level_id, request.ignored
            )
        )
        return await self.list(request.instrument_id)
