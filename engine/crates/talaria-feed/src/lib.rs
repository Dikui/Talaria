//! Feed supervisor state machine, gap/no-trades synthesis, and deterministic JSONL replay.

pub mod clock;
pub mod compaction;
pub mod error;
pub mod replay;
pub mod sink;
pub mod supervisor;

pub use clock::{FeedClock, ManualClock, StepClock, SystemClock};
pub use compaction::{MAX_GAP_FILL, compact_series};
pub use error::FeedError;
pub use replay::{
    DEFAULT_DATA_DIR, DEFAULT_IDLE_HEARTBEAT_MS, ReplayConfig, ReplayLine, ReplayLineKind,
    ReplayTransport, data_dirs_overlap, read_replay_file, validate_replay_data_dir,
    validate_replay_data_dir_with_env,
};
pub use sink::{BarPublishSink, NoopBarPublishSink};
pub use supervisor::{
    BACKOFF_INITIAL_MS, BACKOFF_MAX_MS, BAR_CLOSE_GRACE_MS, DEFAULT_STALE_AFTER_MS,
    FeedSupervisor, FeedSupervisorConfig, MAX_BACKFILL_BARS, TICK_INTERVAL_MS,
};
