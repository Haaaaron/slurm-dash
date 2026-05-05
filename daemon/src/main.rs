use std::net::SocketAddr;
use std::sync::Arc;

use anyhow::Result;
use axum::Router;
use clap::Parser;
use tower_http::compression::CompressionLayer;
use tower_http::trace::TraceLayer;

mod config;
mod db;
mod jobs;
mod models;
mod routes;
mod slurm;
mod snapshot;
mod ssh;
mod state;
mod sync;
mod views;

use state::AppState;

#[derive(Parser, Debug)]
#[command(name = "slurm-dash-daemon", about = "Rust daemon for slurm-dash")]
struct Cli {
    /// Host to bind on
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    /// Port to bind on
    #[arg(long, short = 'p', default_value_t = 8765)]
    port: u16,
    /// Disable background sync loop (do not poll squeue/sacct)
    #[arg(long)]
    no_sync: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,slurm_dash_daemon=debug,tower_http=info".into()),
        )
        .init();

    let cli = Cli::parse();

    let state = Arc::new(AppState::initialize().await?);

    if !cli.no_sync {
        state::spawn_background_loop(state.clone());
    }

    let app: Router = routes::router(state.clone())
        .layer(CompressionLayer::new())
        .layer(TraceLayer::new_for_http());

    let addr: SocketAddr = format!("{}:{}", cli.host, cli.port).parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("slurm-dash-daemon listening on http://{}", addr);
    axum::serve(listener, app).await?;
    Ok(())
}
