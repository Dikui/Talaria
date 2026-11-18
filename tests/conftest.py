"""共享测试夹具：确定性 Bybit 原始响应与仓库路径。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "bybit"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR


def load_fixture(symbol: str, timeframe: str) -> dict:
    path = FIXTURE_DIR / symbol / f"{timeframe}.json"
    return json.loads(path.read_text(encoding="utf-8"))
