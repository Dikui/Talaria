from __future__ import annotations

import json
from pathlib import Path

from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings


def test_openapi_matches_before_router_split(tmp_path: Path) -> None:
    snapshot = Path(__file__).with_name("openapi_before_router_split.json")
    expected = json.loads(snapshot.read_text(encoding="utf-8"))
    app = create_app(AppSettings(data_dir=tmp_path / "data", dist_dir=tmp_path / "dist"))

    assert app.openapi() == expected
