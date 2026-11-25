//! `talaria-data` CLI entrypoint for the Rust Resident Data Service.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::bail;
use clap::{Args, Parser, Subcommand, ValueEnum};
use talaria_contracts::RawBar;
use talaria_feed::DEFAULT_STALE_AFTER_MS;
use talaria_packs::{
    BackfillWindow, DEFAULT_PACK_NAME, HistoryConnector, HistoryConnectorConfig, backfill_bars,
    binance_perp::archive::{BinanceVisionArchiveConnector, DEFAULT_ARCHIVE_BASE_URL},
    get_market_pack,
};
use talaria_server::security::resolve_data_dir;
use talaria_server::{ServerConfig, start_server};
use talaria_store::{ColdStore, DataDirLock, partition_month};
use tokio::io::AsyncReadExt;

#[derive(Debug, Parser)]
#[command(
    name = "talaria-data",
    version,
    about = "Talaria pure-data resident service"
)]
struct Cli {
    /// Target runtime data directory (`<data-dir>/runtime/service.lock`, `data/`, `state/`).
    #[arg(long, global = true)]
    data_dir: Option<PathBuf>,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Clone, Args)]
struct ServeArgs {
    /// Bind host address (strictly loopback, default 127.0.0.1).
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    /// Bind TCP port (`0` allocates an ephemeral loopback port).
    #[arg(long, default_value_t = 0)]
    port: u16,

    /// Market pack identifier.
    #[arg(long, default_value = DEFAULT_PACK_NAME)]
    pack: String,

    /// Path to compiled workbench SPA `dist` directory served at `/desk`.
    #[arg(long, alias = "dist-dir")]
    desk_dist: Option<PathBuf>,

    /// Exit cleanly when parent process dies or closes its stdin pipe.
    #[arg(long, default_value_t = false)]
    exit_with_parent: bool,

    /// Replay JSONL event script path (must use an isolated non-production `--data-dir`).
    #[arg(long, alias = "replay-events")]
    replay_file: Option<PathBuf>,

    /// Replay playback speed multiplier (`1.0` = real-time, `>1.0` faster).
    #[arg(long, default_value_t = 1.0)]
    replay_speed: f64,

    /// Offline REST fixture directory passed to the market pack HistoryConnector.
    #[arg(long)]
    fixture_dir: Option<PathBuf>,

    /// Override upstream exchange REST base URL.
    #[arg(long, alias = "bybit-base-url")]
    history_base_url: Option<String>,

    /// Upstream real-time channel quota shared across strategy/chart/alerts/watchlist.
    #[arg(long, default_value_t = 8)]
    realtime_quota: usize,

    /// Initial backfill bar budget hint.
    #[arg(long, default_value_t = 300)]
    backfill_bars: usize,

    /// Channel staleness threshold in UTC milliseconds.
    #[arg(long, default_value_t = DEFAULT_STALE_AFTER_MS)]
    stale_after_ms: i64,

    /// Solo daemon mode flag (accepted for compatibility with DSH/Python launchers).
    #[arg(long, default_value_t = false)]
    solo: bool,
}

#[derive(Debug, Args)]
struct BackfillArgs {
    /// Market pack identifier.
    #[arg(long, default_value = DEFAULT_PACK_NAME)]
    pack: String,

    /// Instrument ID to backfill (omit to backfill all pack instruments).
    #[arg(long)]
    instrument: Option<String>,

    /// Bar timeframe (e.g. `1m`, `5m`, `1h`).
    #[arg(long, default_value = "1m")]
    timeframe: String,

    /// Start UTC timestamp in milliseconds.
    #[arg(long)]
    start: i64,

    /// End UTC timestamp in milliseconds (defaults to now).
    #[arg(long)]
    end: Option<i64>,

    /// Offline fixture directory for deterministic backfill.
    #[arg(long)]
    fixture_dir: Option<PathBuf>,

    /// Override upstream REST base URL.
    #[arg(long, alias = "bybit-base-url")]
    history_base_url: Option<String>,

    /// History source backend (`rest` or `archive` for `data.binance.vision` bulk ZIPs).
    #[arg(long, value_enum, default_value_t = BackfillSource::Rest)]
    source: BackfillSource,

    /// Compact cold partitions after writing.
    #[arg(long, default_value_t = false)]
    compact: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum BackfillSource {
    Rest,
    Archive,
}

#[derive(Debug, Args)]
struct ContractCheckArgs {
    /// Market pack identifier.
    #[arg(long, default_value = DEFAULT_PACK_NAME)]
    pack: String,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Start the resident data service.
    #[command(visible_alias = "serve")]
    Up(ServeArgs),
    /// Run deterministic JSONL replay into an isolated data directory.
    Replay(ServeArgs),
    /// Backfill historical cold store partitions.
    Backfill(BackfillArgs),
    /// Verify contract & runtime storage compatibility.
    ContractCheck(ContractCheckArgs),
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_tracing();
    let cli = Cli::parse();

    let default_dir = default_data_dir();
    match cli.command {
        Some(Command::Up(args)) => {
            let data_dir = cli.data_dir.unwrap_or(default_dir);
            run_server(data_dir, args, false).await
        }
        Some(Command::Replay(mut args)) => {
            if args.replay_file.is_none() {
                bail!("`replay` subcommand requires `--replay-file <PATH>`");
            }
            let data_dir = cli.data_dir.ok_or_else(|| {
                anyhow::anyhow!(
                    "`replay` requires an explicit isolated `--data-dir` (cannot touch default .talaria)"
                )
            })?;
            args.solo = true;
            run_server(data_dir, args, true).await
        }
        Some(Command::Backfill(args)) => {
            let data_dir = cli.data_dir.unwrap_or(default_dir);
            run_backfill(data_dir, args).await
        }
        Some(Command::ContractCheck(args)) => run_contract_check(cli.data_dir, args),
        None => {
            let data_dir = cli.data_dir.unwrap_or(default_dir);
            let default_serve = ServeArgs {
                host: "127.0.0.1".to_owned(),
                port: 0,
                pack: DEFAULT_PACK_NAME.to_owned(),
                desk_dist: None,
                exit_with_parent: false,
                replay_file: None,
                replay_speed: 1.0,
                fixture_dir: None,
                history_base_url: None,
                realtime_quota: 8,
                backfill_bars: 300,
                stale_after_ms: DEFAULT_STALE_AFTER_MS,
                solo: false,
            };
            run_server(data_dir, default_serve, false).await
        }
    }
}

fn init_tracing() {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .try_init();
}

fn default_data_dir() -> PathBuf {
    if let Ok(raw) = std::env::var("TALARIA_DATA_DIR")
        && !raw.trim().is_empty()
    {
        return PathBuf::from(raw);
    }
    PathBuf::from(".talaria")
}

fn system_now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

async fn run_server(data_dir: PathBuf, args: ServeArgs, force_replay: bool) -> anyhow::Result<()> {
    if force_replay && args.replay_file.is_none() {
        bail!("replay mode requires --replay-file");
    }

    let mut cfg = ServerConfig::new(&data_dir);
    cfg.host = args.host;
    cfg.port = args.port;
    cfg.pack = args.pack;
    cfg.desk_dist = args.desk_dist;
    cfg.replay_file = args.replay_file;
    cfg.replay_speed = args.replay_speed;
    cfg.fixture_dir = args.fixture_dir;
    cfg.history_base_url = args.history_base_url;
    cfg.realtime_quota = args.realtime_quota;
    cfg.stale_after_ms = args.stale_after_ms;
    let _ = args.backfill_bars;

    let server = start_server(cfg)
        .await
        .map_err(|err| anyhow::anyhow!("{}: {}", err.code, err.message))?;

    let ready_line = serde_json::to_string(&server.descriptor)?;
    println!("{ready_line}");
    let _ = std::io::stdout().flush();

    wait_for_shutdown(args.exit_with_parent).await;
    server.shutdown().await;
    Ok(())
}

async fn wait_for_shutdown(exit_with_parent: bool) {
    let parent_future = async move {
        if !exit_with_parent {
            std::future::pending::<()>().await;
            return;
        }
        watch_parent_exit().await;
    };

    tokio::select! {
        _ = wait_for_os_signal() => {}
        _ = parent_future => {}
    }
}

async fn wait_for_os_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let sigint = tokio::signal::ctrl_c();
        let mut sigterm =
            signal(SignalKind::terminate()).expect("failed to register SIGTERM handler");
        tokio::select! {
            _ = sigint => {}
            _ = sigterm.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

async fn watch_parent_exit() {
    #[cfg(unix)]
    let initial_ppid = std::os::unix::process::parent_id();

    let watch_stdin = stdin_is_pipe_or_socket();
    let stdin_fut = async move {
        if !watch_stdin {
            std::future::pending::<()>().await;
            return;
        }
        let mut stdin = tokio::io::stdin();
        let mut buf = [0u8; 64];
        loop {
            match stdin.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
        }
    };

    let ppid_fut = async move {
        #[cfg(unix)]
        {
            if initial_ppid <= 1 {
                return;
            }
            loop {
                tokio::time::sleep(Duration::from_millis(200)).await;
                let cur = std::os::unix::process::parent_id();
                if cur != initial_ppid || cur <= 1 {
                    break;
                }
            }
        }
        #[cfg(not(unix))]
        {
            std::future::pending::<()>().await;
        }
    };

    tokio::select! {
        _ = stdin_fut => {}
        _ = ppid_fut => {}
    }
}

fn stdin_is_pipe_or_socket() -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileTypeExt;
        if let Ok(meta) = std::fs::File::open("/dev/fd/0").and_then(|f| f.metadata()) {
            let ft = meta.file_type();
            return ft.is_fifo() || ft.is_socket();
        }
        false
    }
    #[cfg(not(unix))]
    {
        false
    }
}

async fn run_backfill(data_dir: PathBuf, args: BackfillArgs) -> anyhow::Result<()> {
    let resolved = resolve_data_dir(&data_dir)?;
    let mut lock = DataDirLock::acquire(&resolved, "backfill")
        .map_err(|e| anyhow::anyhow!("failed to lock {}: {e}", resolved.display()))?;
    let pack = get_market_pack(&args.pack)?;
    let connector: Arc<dyn HistoryConnector> = match args.source {
        BackfillSource::Rest => pack.create_history_connector(HistoryConnectorConfig {
            base_url: args.history_base_url,
            fixture_dir: args.fixture_dir,
            now_ms: Arc::new(system_now_ms),
            ..HistoryConnectorConfig::default()
        })?,
        BackfillSource::Archive => {
            if args.pack != "binance_perp" {
                bail!("`--source archive` is currently supported only for `--pack binance_perp`");
            }
            let base = args
                .history_base_url
                .unwrap_or_else(|| DEFAULT_ARCHIVE_BASE_URL.to_owned());
            Arc::new(BinanceVisionArchiveConnector::new(
                base,
                Duration::from_secs(30),
                Arc::new(system_now_ms),
            )?)
        }
    };
    let store = ColdStore::open(&resolved)?;

    let end = args.end.unwrap_or_else(system_now_ms);
    let specs: Vec<_> = match &args.instrument {
        Some(id) => vec![
            pack.instrument(id)
                .cloned()
                .ok_or_else(|| anyhow::anyhow!("unknown instrument: {id}"))?,
        ],
        None => pack.instruments().to_vec(),
    };

    let mut total_bars = 0usize;
    for spec in &specs {
        let rows = backfill_bars(
            connector.as_ref(),
            &spec.symbol,
            &args.timeframe,
            BackfillWindow {
                start_ts: Some(args.start),
                end_ts: Some(end),
                ..BackfillWindow::default()
            },
        )
        .await?;
        let mut by_month: BTreeMap<String, Vec<RawBar>> = BTreeMap::new();
        for bar in rows {
            total_bars += 1;
            let month = partition_month(bar.ts_event).unwrap_or_else(|_| "unknown".to_owned());
            by_month.entry(month).or_default().push(bar);
        }
        for (_, mut batch) in by_month {
            batch.sort_by_key(|b| (b.ts_event, b.revision));
            store.publish_bars(spec, &args.timeframe, &batch)?;
        }
    }

    if args.compact {
        store.compact(&specs, None)?;
    }
    store.close()?;
    lock.release();

    let out = serde_json::json!({
        "ok": true,
        "data_dir": resolved.to_string_lossy(),
        "pack": args.pack,
        "timeframe": args.timeframe,
        "bars": total_bars,
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}

fn run_contract_check(data_dir: Option<PathBuf>, args: ContractCheckArgs) -> anyhow::Result<()> {
    let pack = get_market_pack(&args.pack)?;
    let instruments = pack.instruments();
    for spec in instruments {
        if spec.instrument_id.is_empty() || spec.tick_size.is_zero() || spec.qty_step.is_zero() {
            bail!("invalid instrument spec {}", spec.instrument_id);
        }
        let _ = pack.traits(&spec.instrument_id)?;
    }
    let storage_ok = match &data_dir {
        Some(dir) => check_existing_data_dir(dir)?,
        None => true,
    };
    let out = serde_json::json!({
        "ok": storage_ok,
        "pack": args.pack,
        "instruments": instruments.iter().map(|s| &s.instrument_id).collect::<Vec<_>>(),
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}

fn check_existing_data_dir(dir: &Path) -> anyhow::Result<bool> {
    if !dir.exists() {
        return Ok(true);
    }
    let _reader = talaria_store::ColdReader::new(dir);
    Ok(true)
}
