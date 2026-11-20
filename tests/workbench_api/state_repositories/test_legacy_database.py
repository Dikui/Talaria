"""任务单 03：旧协同状态库无需迁移即可读取。"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.workbench_api.state_store import STATE_DB, StateWriterQueue

pytestmark = [pytest.mark.medium, pytest.mark.regression]

_LEGACY_ROWS = """
INSERT INTO meta VALUES ('desk', '{"layout":"classic"}', 1000);
INSERT INTO events VALUES (7, 'note', '{"text":"保留"}', 1000);
INSERT INTO drawings VALUES (
    'drw_old', 'BTC-USDT-PERP', 'HORIZONTAL_LINE', '["1h"]',
    '[{"ts":0,"price":"101.10"}]', '旧画线', '{}', 2, 1000, 2000, NULL
);
INSERT INTO drawing_versions VALUES (
    'drw_old', 1, 'BTC-USDT-PERP', 'HORIZONTAL_LINE', '["1h"]',
    '[{"ts":0,"price":"100.10"}]', '更早的画线', '{}', 1000, 1000, NULL
);
INSERT INTO watchlist_groups VALUES ('grp_old', '旧自选', 0, 'MANUAL', 0, 1000, 1000);
INSERT INTO watchlist_items VALUES ('grp_old', 'BTC-USDT-PERP', 0, 1000, 1000);
INSERT INTO key_level_ignores VALUES ('BTC-USDT-PERP', 'ignored', 1, 1000);
INSERT INTO key_level_adoptions VALUES (
    'BTC-USDT-PERP', 'adopted', 'snap_old', 'drw_old', 1000, '1h'
);
INSERT INTO alerts VALUES (
    'alert_old', 'BTC-USDT-PERP', '1h', 'ARMED', 1, NULL, 1000, 1000, NULL,
    '{"alert_id":"alert_old","instrument_id":"BTC-USDT-PERP","timeframe":"1h",
      "condition":{"kind":"METRIC_THRESHOLD","metric":"last_price","direction":"ABOVE",
                   "threshold":"101.10"},
      "status":"ARMED","repeat":true,"cooldown_ms":1000,"created_ts":1000,"updated_ts":1000}'
);
INSERT INTO alert_triggers VALUES (
    'trigger_old', 'alert_old', 'BTC-USDT-PERP', '1h', 1000, 'snap_old',
    '{"trigger_id":"trigger_old","alert_id":"alert_old","instrument_id":"BTC-USDT-PERP",
      "timeframe":"1h","condition_kind":"METRIC_THRESHOLD","triggered_ts":1000,
      "snapshot_id":"snap_old","observed":"102.00","message":"旧预警"}',
    1000
);
INSERT INTO agent_annotations VALUES (
    'ann_old', 'BTC-USDT-PERP', 'snap_old', '1h', 'session_old', 0, 'LEVEL',
    'ADOPTED', 1000, 1000, 2000, 'drw_old', NULL,
    '{"annotation_id":"ann_old","instrument_id":"BTC-USDT-PERP","snapshot_id":"snap_old",
      "timeframe":"1h","session_id":"session_old","round":0,"kind":"LEVEL","status":"ADOPTED",
      "label":"旧标注","rationale":"","price_low":"101.10","price_high":"101.10",
      "ts_anchor":1000,"evidence_refs":[{"level_id":"adopted"}],"created_ts":1000,
      "updated_ts":2000,"adopted_drawing_id":"drw_old"}'
);
INSERT INTO annotation_adoptions VALUES (
    'ann_old', 'BTC-USDT-PERP', 'snap_old', '1h', 'session_old', 0, 'drw_old', 2000,
    '{"annotation_id":"ann_old","instrument_id":"BTC-USDT-PERP","snapshot_id":"snap_old",
      "timeframe":"1h","session_id":"session_old","round":0,"drawing_id":"drw_old","adopted_ts":2000}'
);
INSERT INTO collaborations VALUES (
    'consult_old', 'snap_old', 'BTC-USDT-PERP', '1h', 'PENDING', NULL, NULL, 1000, 1000,
    '{"consultation_id":"consult_old","snapshot_id":"snap_old","instrument_id":"BTC-USDT-PERP",
      "timeframe":"1h","status":"PENDING","created_ts":1000,"updated_ts":1000,
      "package":{"consultation_id":"consult_old","snapshot_id":"snap_old",
                 "instrument_id":"BTC-USDT-PERP","timeframe":"1h","question":"旧会诊",
                 "visible":{"from_ts":0,"to_ts":1000,"price_min":"99","price_max":"110"},
                 "snapshot_cutoff_ts":1000,"created_ts":1000}}',
    '旧提示词'
);
INSERT INTO scanner_briefings VALUES (
    'brief_old', 'trigger_old', 'snap_old', 'BTC-USDT-PERP', '1h', 'READY',
    '{"trigger_reason":"旧简报"}', '[]', NULL, 1000, 2000
);
INSERT INTO token_usage VALUES ('session_old', 'turn_old', 120, NULL, 30, 'host', 1000);
"""


def _schema(db_path: Path) -> list[tuple[str, str, str | None]]:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def test_legacy_database_round_trips_through_repositories(tmp_path: Path) -> None:
    legacy_db = tmp_path / "legacy" / STATE_DB
    legacy_db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(legacy_db)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(Path(__file__).with_name("legacy_schema.sql").read_text())
        connection.executescript(_LEGACY_ROWS)
    legacy_schema = _schema(legacy_db)

    async def scenario() -> None:
        async with StateWriterQueue(tmp_path / "legacy") as queue:
            assert await queue.read(lambda store: store.meta.get("desk")) == {
                "layout": "classic"
            }
            drawing = await queue.read(lambda store: store.drawings.get("drw_old"))
            assert drawing.version == 2
            assert drawing.points[0].price == Decimal("101.10")
            versions = await queue.read(
                lambda store: store.drawings.list_versions("drw_old")
            )
            assert [item.points[0].price for item in versions] == [Decimal("100.10")]
            annotation = await queue.read(lambda store: store.annotations.get("ann_old"))
            assert annotation.label == "旧标注"
            adoptions = await queue.read(
                lambda store: store.annotations.list_adoptions()
            )
            assert [item.drawing_id for item in adoptions] == ["drw_old"]
            watchlist = await queue.read(lambda store: store.watchlist.list())
            assert [group.name for group in watchlist.groups] == ["旧自选"]
            assert [item.instrument_id for item in watchlist.items_for("grp_old")] == [
                "BTC-USDT-PERP"
            ]
            ignores = await queue.read(
                lambda store: store.key_level_feedback.list_ignores("BTC-USDT-PERP")
            )
            assert [item["ignored"] for item in ignores] == [True]
            adoption = await queue.read(
                lambda store: store.key_level_feedback.get_adoption(
                    "BTC-USDT-PERP", "adopted"
                )
            )
            assert adoption is not None and adoption["drawing_id"] == "drw_old"
            alert = await queue.read(lambda store: store.alerts.get("alert_old"))
            assert alert.condition.threshold == Decimal("101.10")
            triggers = await queue.read(lambda store: store.alerts.list_triggers())
            assert [item.message for item in triggers] == ["旧预警"]
            consultation = await queue.read(
                lambda store: store.consultations.get("consult_old")
            )
            assert consultation.package.question == "旧会诊"
            assert await queue.read(
                lambda store: store.consultations.get_prompt("consult_old")
            ) == "旧提示词"
            briefing = await queue.read(lambda store: store.briefings.get("brief_old"))
            assert briefing.summary is not None
            assert briefing.summary.trigger_reason == "旧简报"
            assert briefing.read_ts == 2000
            usage = await queue.read(lambda store: store.usage.list())
            assert [(row.input_tokens, row.cached_input_tokens) for row in usage] == [
                (120, None)
            ]
            assert await queue.submit("note", {}) == 8

        async with StateWriterQueue(tmp_path / "fresh"):
            pass

    asyncio.run(scenario())

    with closing(sqlite3.connect(legacy_db)) as connection:
        assert connection.execute(
            "SELECT payload FROM events WHERE event_id = 7"
        ).fetchone() == ('{"text":"保留"}',)
    assert _schema(legacy_db) == legacy_schema
    # 全新库的表、列、索引和外键也必须与旧库使用相同的定义。
    assert _schema(tmp_path / "fresh" / STATE_DB) == legacy_schema
