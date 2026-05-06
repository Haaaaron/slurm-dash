use anyhow::{Context, Result};
use std::path::{Path, PathBuf};

pub struct PidFile {
    path: PathBuf,
}

impl PidFile {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn write(path: &Path, pid: u32) -> Result<PidFile> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("failed to create pidfile parent dir: {}", parent.display()))?;
        }
        std::fs::write(path, pid.to_string())
            .with_context(|| format!("failed to write pidfile: {}", path.display()))?;
        Ok(PidFile { path: path.to_path_buf() })
    }

    pub fn read(path: &Path) -> Option<u32> {
        std::fs::read_to_string(path)
            .ok()
            .and_then(|s| s.trim().parse::<u32>().ok())
    }

    #[cfg(unix)]
    pub fn is_alive(pid: u32) -> bool {
        unsafe { libc::kill(pid as i32, 0) == 0 }
    }

    #[cfg(not(unix))]
    pub fn is_alive(pid: u32) -> bool {
        use std::process::Command;
        Command::new("tasklist")
            .args(&["/FI", &format!("PID eq {}", pid)])
            .output()
            .ok()
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .map(|output| output.contains(&pid.to_string()))
            .unwrap_or(false)
    }

    pub fn remove(&self) -> std::io::Result<()> {
        std::fs::remove_file(&self.path)
    }
}

impl Drop for PidFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}
