//! `talaria-data` CLI entrypoint for the Rust Resident Service.

use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(name = "talaria-data", version, about = "Talaria pure-data resident service")]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Start the resident data service.
    Up,
    /// Run deterministic JSONL replay into an isolated data directory.
    Replay,
    /// Backfill historical cold store partitions.
    Backfill,
    /// Verify contract compatibility.
    ContractCheck,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let _cli = Cli::parse();
    Ok(())
}
