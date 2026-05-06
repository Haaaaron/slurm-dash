//! Thin async wrappers around the system `ssh`/`scp` commands. Mirrors the
//! Python `remote_manager.py` so user `~/.ssh/config`, ProxyJump, and agent
//! forwarding all keep working without re-implementation.

use std::time::Duration;

use anyhow::{anyhow, Result};
use tokio::process::Command;

const SSH_OPTS: &[&str] = &[
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2",
    "-o", "BatchMode=yes",
];

#[derive(Debug, Clone)]
pub struct SshOutput {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
}

pub async fn run_ssh(host: &str, command: &str, timeout: Duration) -> Result<SshOutput> {
    let mut cmd = Command::new("ssh");
    cmd.args(SSH_OPTS);
    cmd.arg(host);
    cmd.arg(command);
    cmd.kill_on_drop(true);
    let fut = cmd.output();
    let out = tokio::time::timeout(timeout, fut)
        .await
        .map_err(|_| anyhow!("ssh to {host} timed out after {:?}", timeout))??;
    Ok(SshOutput {
        success: out.status.success(),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    })
}
