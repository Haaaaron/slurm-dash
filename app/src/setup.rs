//! Remote server setup: add, remove, and auto-install logic.

use anyhow::{Context, Result};
use base64::Engine;
use std::time::Duration;

use crate::config;
use crate::db::Db;
use crate::ssh::run_ssh;

const CAPTURE_PY: &[u8] = include_bytes!("../assets/capture.py");
#[allow(dead_code)]
const SBATCH_WRAPPER: &str = include_str!("../assets/sbatch_wrapper.sh.tmpl");
const INIT_DB_PY: &str = include_str!("../assets/init_db.py");
const INJECT_INTERCEPTOR_PY: &str = include_str!("../assets/inject_interceptor.py");

pub async fn add_target(ssh_string: &str, alias: &str) -> Result<()> {
    tracing::info!("adding target {alias} ({ssh_string})");

    run_ssh(
        ssh_string,
        "mkdir -p ~/.slurm_tracker/snapshots ~/.slurm_tracker/staging ~/.slurm_tracker/bin",
        Duration::from_secs(30),
    )
    .await
    .context("failed to create remote directories")?;

    let b64 = base64::engine::general_purpose::STANDARD.encode(CAPTURE_PY);
    let cmd = format!("echo '{b64}' | base64 -d > ~/.slurm_tracker/capture.py && chmod +x ~/.slurm_tracker/capture.py");
    run_ssh(ssh_string, &cmd, Duration::from_secs(30))
        .await
        .context("failed to deploy capture.py")?;

    let init_cmd = format!("python3 -c {}", shell_escape::unix::escape(INIT_DB_PY.into()));
    run_ssh(ssh_string, &init_cmd, Duration::from_secs(30))
        .await
        .context("failed to initialize remote DB")?;

    let inject_cmd = format!("python3 -c {}", shell_escape::unix::escape(INJECT_INTERCEPTOR_PY.into()));
    run_ssh(ssh_string, &inject_cmd, Duration::from_secs(30))
        .await
        .context("failed to inject sbatch interceptor")?;

    Ok(())
}

pub async fn remove_target(
    ssh_string: &str,
    alias: &str,
    purge_local: bool,
    db: Option<&Db>,
) -> Result<()> {
    let alias_owned = alias.to_string();
    tracing::info!("removing target {}", alias);

    let awk_cmd = r#"awk '/# --- SLURM DASH INTERCEPTOR ---/{skip=1} !skip{print} /# --- END SLURM DASH INTERCEPTOR ---/{skip=0; next}'"#;

    for rc in &["~/.bashrc", "~/.zshrc"] {
        let cmd = format!(
            "if [ -f {rc} ] && grep -q 'SLURM DASH INTERCEPTOR' {rc}; then tmp=$(mktemp) && {awk_cmd} {rc} > \"$tmp\" && mv \"$tmp\" {rc}; fi"
        );
        let _ = run_ssh(ssh_string, &cmd, Duration::from_secs(30)).await;
    }

    let _ = run_ssh(
        ssh_string,
        "rm -rf ~/.slurm_tracker",
        Duration::from_secs(30),
    )
    .await;

    config::write_server_remove(&crate::config::Paths::resolve()?.config_path, alias)
        .context("failed to remove server from config")?;

    if purge_local {
        if let Some(db) = db {
            db.with(move |conn| {
                crate::db::purge_alias(conn, &alias_owned).map(|_| ())
            })
            .await
            .ok();
        }
    }

    Ok(())
}

pub async fn ensure_installed(ssh_string: &str, alias: &str) -> Result<bool> {
    let check_cmd = "test -f ~/.slurm_tracker/capture.py && echo ok";
    let result = run_ssh(ssh_string, check_cmd, Duration::from_secs(10))
        .await
        .ok()
        .map(|r| r.success && r.stdout.trim() == "ok")
        .unwrap_or(false);

    if result {
        return Ok(false);
    }

    tracing::info!("auto-installing interceptor on {alias}");
    add_target(ssh_string, alias)
        .await
        .context(format!("auto-install failed for {alias}"))?;
    Ok(true)
}
