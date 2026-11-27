//! Local loopback Axum HTTP (Arrow IPC + JSON), dual-semantic WebSocket, and workbench state store.

pub mod alerts;
pub mod cold_writer;
pub mod error;
pub mod key_levels;
pub mod paper_watcher;
pub mod routes;
pub mod rules;
pub mod scanner;
pub mod security;
pub mod snapshot_store;
pub mod state_store;
pub mod structures;
pub mod subscriptions;
pub mod ws;

pub mod server;

pub use server::{AppState, RunningServer, ServerConfig, start_server};

pub fn crate_name() -> &'static str {
    "talaria-server"
}
