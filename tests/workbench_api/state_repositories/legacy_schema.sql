-- 任务单 03：保留拆分前的协同状态库建表语句。
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS drawings (
    drawing_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible_timeframes TEXT NOT NULL,
    points TEXT NOT NULL,
    text TEXT NOT NULL,
    style TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX IF NOT EXISTS drawings_by_instrument
    ON drawings (instrument_id, deleted_at);
CREATE TABLE IF NOT EXISTS drawing_versions (
    drawing_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible_timeframes TEXT NOT NULL,
    points TEXT NOT NULL,
    text TEXT NOT NULL,
    style TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER,
    PRIMARY KEY (drawing_id, version)
);
CREATE TABLE IF NOT EXISTS watchlist_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    is_system INTEGER NOT NULL CHECK (is_system IN (0, 1)),
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_items (
    group_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (group_id, instrument_id),
    FOREIGN KEY (group_id) REFERENCES watchlist_groups(group_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS watchlist_items_group_idx
    ON watchlist_items(group_id, position);
CREATE TABLE IF NOT EXISTS key_level_ignores (
    instrument_id TEXT NOT NULL,
    level_id TEXT NOT NULL,
    ignored INTEGER NOT NULL CHECK (ignored IN (0, 1)),
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, level_id)
);
CREATE INDEX IF NOT EXISTS key_level_ignores_by_instrument
    ON key_level_ignores (instrument_id, updated_ts);
CREATE TABLE IF NOT EXISTS key_level_adoptions (
    instrument_id TEXT NOT NULL,
    level_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    adopted_ts INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    PRIMARY KEY (instrument_id, level_id)
);
CREATE INDEX IF NOT EXISTS key_level_adoptions_by_instrument
    ON key_level_adoptions (instrument_id, adopted_ts DESC);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    repeat_flag INTEGER NOT NULL CHECK (repeat_flag IN (0, 1)),
    next_arm_ts INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_by_instrument
    ON alerts (instrument_id, deleted_at);
CREATE INDEX IF NOT EXISTS alerts_by_status
    ON alerts (status, deleted_at);
CREATE TABLE IF NOT EXISTS alert_triggers (
    trigger_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    triggered_ts INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_triggers_by_time
    ON alert_triggers (triggered_ts DESC);
CREATE INDEX IF NOT EXISTS alert_triggers_by_alert
    ON alert_triggers (alert_id, triggered_ts DESC);
CREATE TABLE IF NOT EXISTS agent_annotations (
    annotation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    session_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    ts_anchor INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    adopted_drawing_id TEXT,
    archived_reason TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_annotations_by_instrument
    ON agent_annotations (instrument_id, status, created_ts);
CREATE INDEX IF NOT EXISTS agent_annotations_by_snapshot
    ON agent_annotations (snapshot_id, status);
CREATE INDEX IF NOT EXISTS agent_annotations_by_session
    ON agent_annotations (session_id, status);
CREATE TABLE IF NOT EXISTS annotation_adoptions (
    annotation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    session_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    drawing_id TEXT NOT NULL,
    adopted_ts INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS annotation_adoptions_by_instrument
    ON annotation_adoptions (instrument_id, adopted_ts DESC);
CREATE TABLE IF NOT EXISTS collaborations (
    consultation_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    host_session_id TEXT,
    host_turn_id TEXT,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    payload TEXT NOT NULL,
    prompt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS collaborations_by_time
    ON collaborations (created_ts DESC);
CREATE TABLE IF NOT EXISTS scanner_briefings (
    id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    evidence_refs TEXT NOT NULL,
    abstain_reason TEXT,
    created_ts INTEGER NOT NULL,
    read_ts INTEGER
);
CREATE INDEX IF NOT EXISTS scanner_briefings_by_time
    ON scanner_briefings (created_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS scanner_briefings_unread
    ON scanner_briefings (read_ts, created_ts DESC);
CREATE TABLE IF NOT EXISTS token_usage (
    dsh_session_id TEXT NOT NULL,
    dsh_turn_id TEXT NOT NULL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    source TEXT NOT NULL,
    reported_ts INTEGER NOT NULL,
    PRIMARY KEY (dsh_session_id, dsh_turn_id)
);
CREATE INDEX IF NOT EXISTS token_usage_by_time
    ON token_usage (reported_ts DESC);
