"""看盘守护进程的运行参数。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from talaria.packs import DEFAULT_PACK
from talaria.workbench_api.service_config import FeatureToggles


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class AppSettings:
    data_dir: Path
    dist_dir: Path
    fixture_dir: Path | None = None
    history_base_url: str | None = None
    replay_events: Path | None = None
    replay_speed: float = 1.0
    stale_after_ms: int = 12_000
    backfill_bars: int = 3000
    max_pages: int = 5
    realtime_quota: int = 8
    token: str | None = None
    now_ms: Callable[[], int] = field(default=_now_ms)
    pack: str = DEFAULT_PACK
    features: FeatureToggles = FeatureToggles()

    def __post_init__(self) -> None:
        if self.backfill_bars <= 0:
            raise ValueError("backfill_bars 必须为正数")
        if self.max_pages <= 0:
            raise ValueError("max_pages 必须为正数")
        if self.realtime_quota <= 0:
            raise ValueError("realtime_quota 必须为正数")
        if self.replay_speed <= 0:
            raise ValueError("replay_speed 必须为正数")
        if self.stale_after_ms <= 0:
            raise ValueError("stale_after_ms 必须为正数")
