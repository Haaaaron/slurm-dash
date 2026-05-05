//! Config + path resolution mirroring the Python `config.py` (platformdirs).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use directories::ProjectDirs;
use serde::Deserialize;

const APP_NAME: &str = "slurm-dash";

#[derive(Debug, Clone, Deserialize, Default)]
pub struct General {
    #[serde(default)]
    pub max_download_mb: Option<u64>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct Server {
    pub ssh_string: Option<String>,
    #[serde(default)]
    pub sync_on_startup: bool,
    #[serde(default)]
    pub alias: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct Config {
    #[serde(default)]
    pub general: General,
    #[serde(default)]
    pub servers: BTreeMap<String, Server>,
}

#[derive(Debug, Clone)]
pub struct Paths {
    pub config_dir: PathBuf,
    pub data_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub config_path: PathBuf,
    pub db_path: PathBuf,
}

impl Paths {
    pub fn resolve() -> Result<Self> {
        // platformdirs in Python uses XDG paths on Linux:
        //   config: $XDG_CONFIG_HOME/slurm-dash         (default ~/.config/slurm-dash)
        //   data:   $XDG_DATA_HOME/slurm-dash           (default ~/.local/share/slurm-dash)
        //   cache:  $XDG_CACHE_HOME/slurm-dash          (default ~/.cache/slurm-dash)
        // `directories` ProjectDirs uses qualifier/organisation/application,
        // which produces the same paths on Linux when qualifier and organisation
        // are empty strings.
        let proj = ProjectDirs::from("", "", APP_NAME)
            .context("could not resolve platform dirs for slurm-dash")?;
        let config_dir = proj.config_dir().to_path_buf();
        let data_dir = proj.data_dir().to_path_buf();
        let cache_dir = proj.cache_dir().to_path_buf();
        let config_path = config_dir.join("config.toml");
        let db_path = data_dir.join("local_state.db");
        Ok(Paths {
            config_dir,
            data_dir,
            cache_dir,
            config_path,
            db_path,
        })
    }
}

pub fn load_config(path: &Path) -> Result<Config> {
    if !path.exists() {
        return Ok(Config::default());
    }
    let bytes = std::fs::read_to_string(path)
        .with_context(|| format!("read config file {}", path.display()))?;
    let cfg: Config = toml::from_str(&bytes)
        .with_context(|| format!("parse config file {}", path.display()))?;
    Ok(cfg)
}
