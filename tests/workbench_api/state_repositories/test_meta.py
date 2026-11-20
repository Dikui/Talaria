"""协同状态库元数据的持久化。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from talaria.workbench_api.state_store import STATE_DB, StateStore

pytestmark = [pytest.mark.medium, pytest.mark.spec]

NOW_MS = 1_795_022_974_103


def test_meta_values_are_upserted(tmp_path: Path) -> None:
    store = StateStore(tmp_path, now_ms=lambda: NOW_MS)
    try:
        store.meta.set("desk", {"layout": "a"})
        store.meta.set("desk", {"layout": "b"})
    finally:
        store.close()

    connection = sqlite3.connect(tmp_path / STATE_DB)
    try:
        rows = connection.execute("SELECT key, value FROM meta").fetchall()
    finally:
        connection.close()
    assert rows == [("desk", '{"layout": "b"}')]
