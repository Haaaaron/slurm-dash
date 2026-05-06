use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use axum::Router;
use clap::{Parser, Subcommand};
use tower_http::compression::CompressionLayer;
use tower_http::trace::TraceLayer;

mod config;
mod db;
mod jobs;
mod launcher;
mod models;
mod pidfile;
mod routes;
mod setup;
mod slurm;
mod snapshot;
mod ssh;
mod state;
mod sync;
mod views;

use config::Paths;
use pidfile::PidFile;
use state::AppState;

#[derive(Parser, Debug)]
#[command(name = "slurm-dash", about = "SLURM job dashboard")]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Start the web server in the foreground (default: starts in background + opens browser)
    Serve {
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, short = 'p', default_value_t = 8765)]
        port: u16,
        #[arg(long)]
        no_sync: bool,
    },
    /// Add a remote SLURM cluster
    Add {
        #[arg(help = "SSH connection string (e.g., user@cluster.edu)")]
        ssh_string: String,
        #[arg(long, help = "Alias for the cluster")]
        alias: Option<String>,
    },
    /// Remove a configured SLURM cluster
    Remove {
        #[arg(help = "Cluster alias or SSH string")]
        alias: String,
        #[arg(long, help = "Also delete local job records")]
        purge_local: bool,
        #[arg(short = 'y', help = "Skip confirmation prompt")]
        yes: bool,
    },
    /// List configured clusters
    List,
    /// Initialize config file with template
    InitConfig,
    /// Check daemon status
    Status,
    /// Stop the background daemon
    Stop,
}

#[tokio::main]
async fn main() -> Result<()> {
    init_logging();

    let cli = Cli::parse();
    let paths = Paths::resolve()?;

    match cli.command {
        None => cmd_default(&paths).await?,
        Some(Command::Serve { host, port, no_sync }) => cmd_serve(host, port, no_sync).await?,
        Some(Command::Add { ssh_string, alias }) => cmd_add(&ssh_string, alias, &paths).await?,
        Some(Command::Remove { alias, purge_local, yes }) => {
            cmd_remove(&alias, purge_local, yes, &paths).await?
        }
        Some(Command::List) => cmd_list(&paths).await?,
        Some(Command::InitConfig) => cmd_init_config(&paths)?,
        Some(Command::Status) => cmd_status(&paths).await?,
        Some(Command::Stop) => cmd_stop(&paths).await?,
    }

    Ok(())
}

fn init_logging() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,slurm_dash=debug,tower_http=info".into()),
        )
        .init();
}

fn pidfile_path(paths: &Paths) -> std::path::PathBuf {
    paths.data_dir.join("daemon.pid")
}

async fn cmd_default(paths: &Paths) -> Result<()> {
    let pidfile_path = pidfile_path(paths);
    let port = 8765u16;

    if let Some(pid) = PidFile::read(&pidfile_path) {
        if PidFile::is_alive(pid) {
            tracing::info!("daemon already running (pid {}, port {})", pid, port);
            launcher::open_browser(port).ok();
            return Ok(());
        }
    }

    tracing::info!("starting daemon...");
    let child_pid = launcher::spawn_daemon(port).await?;
    PidFile::write(&pidfile_path, child_pid).ok();

    tracing::info!("waiting for server to start...");
    launcher::wait_for_server(port, Duration::from_secs(5)).await?;

    launcher::open_browser(port)?;
    Ok(())
}

async fn cmd_serve(host: String, port: u16, no_sync: bool) -> Result<()> {
    let paths = Paths::resolve()?;
    let state = Arc::new(AppState::initialize(&paths).await?);

    let pidfile = {
        let pidfile_path = pidfile_path(&paths);
        Some(PidFile::write(&pidfile_path, std::process::id())?)
    };

    if !no_sync {
        state::spawn_background_loop(state.clone(), &paths).await?;
    }

    let app: Router = routes::router(state.clone())
        .layer(CompressionLayer::new())
        .layer(TraceLayer::new_for_http());

    let addr: SocketAddr = format!("{}:{}", host, port).parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("slurm-dash listening on http://{}", addr);

    axum::serve(listener, app).await?;
    drop(pidfile);
    Ok(())
}

async fn cmd_add(ssh_string: &str, alias: Option<String>, paths: &Paths) -> Result<()> {
    let alias = alias.unwrap_or_else(|| ssh_string.split('@').next_back().unwrap_or("cluster").to_string());

    println!("Adding cluster {}...", alias);
    setup::add_target(ssh_string, &alias)
        .await
        .context("failed to set up remote")?;
    config::write_server_add(&paths.config_path, &alias, ssh_string)?;
    println!("✓ Cluster {} added. Restart slurm-dash to apply.", alias);
    Ok(())
}

async fn cmd_remove(alias: &str, purge_local: bool, yes: bool, paths: &Paths) -> Result<()> {
    let config = config::load_config(&paths.config_path)?;
    let ssh_string = config
        .servers
        .get(alias)
        .and_then(|s| s.ssh_string.clone())
        .context(format!("cluster '{}' not found in config", alias))?;

    if !yes {
        println!("Will remove cluster '{}' from {}:", alias, paths.config_path.display());
        println!("  - strip interceptor from ~/.bashrc and ~/.zshrc on remote");
        println!("  - delete ~/.slurm_tracker on remote");
        println!("  - remove [servers.{}] from config", alias);
        if purge_local {
            println!("  - delete local job records");
        }
        println!();
        match prompt_yn() {
            true => println!("Proceeding..."),
            false => {
                println!("Cancelled.");
                return Ok(());
            }
        }
    }

    let db = db::Db::open(&paths.db_path).await.ok();
    setup::remove_target(&ssh_string, alias, purge_local, db.as_ref())
        .await
        .context("failed to remove remote")?;

    println!("✓ Cluster '{}' removed.", alias);
    Ok(())
}

async fn cmd_list(paths: &Paths) -> Result<()> {
    let config = config::load_config(&paths.config_path)?;

    if config.servers.is_empty() {
        println!("No clusters configured.");
        println!("Run: slurm-dash add <ssh_string> --alias <name>");
    } else {
        println!("Configured clusters:");
        for (alias, server) in &config.servers {
            if let Some(ssh) = &server.ssh_string {
                println!("  {} -> {}", alias, ssh);
            }
        }
    }
    Ok(())
}

fn cmd_init_config(paths: &Paths) -> Result<()> {
    config::write_template_config(&paths.config_path)?;
    println!("✓ Config template written to: {}", paths.config_path.display());
    Ok(())
}

async fn cmd_status(paths: &Paths) -> Result<()> {
    let pidfile_path = pidfile_path(paths);

    match PidFile::read(&pidfile_path) {
        Some(pid) if PidFile::is_alive(pid) => {
            println!("running (pid {}, port 8765)", pid);
        }
        _ => {
            println!("not running");
        }
    }
    Ok(())
}

async fn cmd_stop(paths: &Paths) -> Result<()> {
    let pidfile_path = pidfile_path(paths);

    match PidFile::read(&pidfile_path) {
        Some(pid) if PidFile::is_alive(pid) => {
            #[cfg(unix)]
            {
                unsafe { libc::kill(pid as i32, libc::SIGTERM) };
            }
            #[cfg(not(unix))]
            {
                use std::process::Command;
                let _ = Command::new("taskkill")
                    .args(&["/PID", &pid.to_string()])
                    .output();
            }
            let _ = std::fs::remove_file(&pidfile_path);
            println!("✓ Daemon stopped.");
        }
        _ => {
            println!("Daemon not running.");
        }
    }
    Ok(())
}

fn prompt_yn() -> bool {
    use std::io::{self, BufRead};
    let stdin = io::stdin();
    let mut line = String::new();
    print!("Proceed? [y/N] ");
    let _ = std::io::Write::flush(&mut std::io::stdout());
    if stdin.lock().read_line(&mut line).is_ok() {
        matches!(line.trim().to_lowercase().as_str(), "y" | "yes")
    } else {
        false
    }
}
