from __future__ import annotations

import asyncio
import threading
from contextlib import closing
from pathlib import Path

import pytest

from talaria.foundation.errors import SingleWriterViolationError
from talaria.perception.cold_store import ColdStore
from talaria.perception.snapshot_store import SnapshotStore
from talaria.workbench_api.runtime import build_runtime
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateStore


def test_runtime_can_start_and_close_without_leaking_resources(
    tmp_path: Path, fixture_dir: Path, repo_root: Path
) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=fixture_dir,
        replay_events=repo_root / "tests/fixtures/realtime/demo_outage.jsonl",
    )
    initial_threads = set(threading.enumerate())

    async def start_and_close() -> None:
        initial_tasks = asyncio.all_tasks()
        runtime = await build_runtime(settings)
        await runtime.aclose()

        assert asyncio.all_tasks() == initial_tasks
        assert not any(
            thread.name.startswith("state-writer")
            for thread in set(threading.enumerate()) - initial_threads
        )
        with (
            closing(ColdStore(settings.data_dir)),
            closing(SnapshotStore(settings.data_dir)),
            closing(StateStore(settings.data_dir)),
        ):
            pass

    asyncio.run(start_and_close())
    assert not set(threading.enumerate()) - initial_threads


def test_runtime_releases_started_resources_when_single_writer_is_taken(
    tmp_path: Path, fixture_dir: Path, repo_root: Path
) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=fixture_dir,
        replay_events=repo_root / "tests/fixtures/realtime/demo_outage.jsonl",
    )

    async def fail_start() -> None:
        initial_tasks = asyncio.all_tasks()
        with pytest.raises(SingleWriterViolationError):
            await build_runtime(settings)
        assert asyncio.all_tasks() == initial_tasks

    with closing(StateStore(settings.data_dir)):
        asyncio.run(fail_start())
        with (
            closing(ColdStore(settings.data_dir)),
            closing(SnapshotStore(settings.data_dir)),
        ):
            pass
