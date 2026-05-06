//! Shared application state + background sync loop.

use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use tokio::sync::{broadcast, RwLock};

use crate::config::{load_config, Config, Paths};
use crate::db::Db;

/// Broadcast events used to drive SSE updates.
#[derive(Debug, Clone)]
pub enum Event {
    /// Jobs table for an alias has changed (or all aliases if alias is None).
    JobsUpdated { alias: Option<String> },
}

pub struct AppState {
    pub paths: Paths,
    pub config: Config,
    pub db: Db,
    /// Cached live status map per alias. Wrapped in RwLock so the SSE-driving
    /// background loop can update it without taking a long DB lock.
    pub live: RwLock<std::collections::HashMap<String, std::collections::HashMap<String, crate::models::LiveStatus>>>,
    pub events: broadcast::Sender<Event>,
}

impl AppState {
    pub async fn initialize(paths: &Paths) -> Result<Self> {
        std::fs::create_dir_all(&paths.config_dir).ok();
        std::fs::create_dir_all(&paths.data_dir).ok();
        std::fs::create_dir_all(&paths.cache_dir).ok();
        let config = load_config(&paths.config_path)?;
        let db = Db::open(&paths.db_path).await?;
        let (tx, _) = broadcast::channel(64);
        Ok(Self {
            paths: paths.clone(),
            config,
            db,
            live: RwLock::new(Default::default()),
            events: tx,
        })
    }

    pub fn server_aliases(&self) -> Vec<String> {
        self.config.servers.keys().cloned().collect()
    }

    pub fn ssh_for(&self, alias: &str) -> Option<String> {
        self.config
            .servers
            .get(alias)
            .and_then(|s| s.ssh_string.clone())
    }
}

/// Spawn the background sync + live status poll loop.
///
/// On each tick:
///   - For each alias with a configured ssh_string, ensure it's installed (auto-install if needed).
///   - Run sync_server.
///   - Fetch get_live_status for that alias and store it.
///   - Finalize any newly-terminal jobs in the DB.
///   - Broadcast a JobsUpdated event so connected SSE clients re-fetch.
pub async fn spawn_background_loop(state: Arc<AppState>, _paths: &Paths) -> Result<()> {
    tokio::spawn(async move {
        // Initial small delay so the server is reachable before first SSH calls.
        tokio::time::sleep(Duration::from_secs(1)).await;
        loop {
            for alias in state.server_aliases() {
                if let Some(ssh_string) = state.ssh_for(&alias) {
                    // Auto-install interceptor if not yet done.
                    if let Err(e) = crate::setup::ensure_installed(&ssh_string, &alias).await {
                        tracing::warn!(alias, error = %e, "auto-install failed");
                        continue;
                    }

                    // Sync new rows from the remote DB.
                    if let Err(e) = crate::sync::sync_server(&state.db, &alias, &ssh_string).await {
                        tracing::warn!(alias, error = %e, "sync_server failed");
                    }

                    // Fetch live status.
                    let alias_clone = alias.clone();
                    let since = state
                        .db
                        .with(move |conn| crate::db::oldest_unfinished(conn, &alias_clone))
                        .await
                        .unwrap_or(None);

                    match crate::slurm::get_live_status(&ssh_string, since).await {
                        Ok(status_map) => {
                            // Persist any terminal-state finalisations.
                            if !status_map.is_empty() {
                                let alias_clone = alias.clone();
                                let map_clone = status_map.clone();
                                let _ = state
                                    .db
                                    .with(move |conn| {
                                        crate::db::finalize_terminal(conn, &alias_clone, &map_clone)
                                    })
                                    .await;
                            }
                            state.live.write().await.insert(alias.clone(), status_map);
                            let _ = state.events.send(Event::JobsUpdated {
                                alias: Some(alias.clone()),
                            });
                        }
                        Err(e) => {
                            tracing::debug!(alias, error = %e, "get_live_status failed");
                        }
                    }
                }
            }

            tokio::time::sleep(Duration::from_secs(30)).await;
        }
    });
    Ok(())
}
