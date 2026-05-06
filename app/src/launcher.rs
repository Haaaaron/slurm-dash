use anyhow::{Context, Result};
use std::net::TcpStream;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use tokio::time::sleep;

pub async fn spawn_daemon(port: u16) -> Result<u32> {
    let exe = std::env::current_exe()
        .context("failed to get current executable path")?;

    let child = Command::new(&exe)
        .args(["serve", "--port", &port.to_string()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("failed to spawn daemon process")?;

    Ok(child.id())
}

pub async fn wait_for_server(port: u16, timeout: Duration) -> Result<()> {
    let start = Instant::now();
    let addr = format!("127.0.0.1:{}", port);

    loop {
        if TcpStream::connect(&addr).is_ok() {
            return Ok(());
        }

        if start.elapsed() > timeout {
            return Err(anyhow::anyhow!("server failed to start within {:?}", timeout));
        }

        sleep(Duration::from_millis(200)).await;
    }
}

pub fn open_browser(port: u16) -> Result<()> {
    let url = format!("http://localhost:{}", port);
    open::that(&url).context("failed to open browser")?;
    Ok(())
}
