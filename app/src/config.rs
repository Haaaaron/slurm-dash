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

#[derive(Debug, Clone, Default)]
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

pub fn write_server_add(config_path: &Path, alias: &str, ssh_string: &str) -> Result<()> {
    if let Some(parent) = config_path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("failed to create config dir: {}", parent.display()))?;
    }

    let existing = if config_path.exists() {
        std::fs::read_to_string(config_path)
            .with_context(|| format!("failed to read config: {}", config_path.display()))?
    } else {
        String::new()
    };

    if existing.contains(&format!("[servers.{alias}]")) {
        return Ok(());
    }

    let entry = format!("\n[servers.{alias}]\nssh_string = \"{ssh_string}\"\n");
    let new_content = existing + &entry;

    std::fs::write(config_path, new_content)
        .with_context(|| format!("failed to write config: {}", config_path.display()))?;

    Ok(())
}

pub fn write_server_remove(config_path: &Path, alias: &str) -> Result<()> {
    if !config_path.exists() {
        return Ok(());
    }

    let content = std::fs::read_to_string(config_path)
        .with_context(|| format!("failed to read config: {}", config_path.display()))?;

    let lines = content.lines().collect::<Vec<_>>();
    let section_header = format!("[servers.{alias}]");
    let mut out = Vec::new();
    let mut skipping = false;

    for line in lines {
        let stripped = line.trim();
        if stripped == section_header {
            skipping = true;
            continue;
        }
        if skipping {
            if stripped.starts_with('[') && stripped.ends_with(']') {
                skipping = false;
                out.push(line);
            }
            continue;
        }
        out.push(line);
    }

    let mut new_content = out.join("\n");
    new_content = regex::Regex::new(r"\n{3,}")
        .unwrap()
        .replace_all(&new_content, "\n\n")
        .to_string();

    std::fs::write(config_path, new_content)
        .with_context(|| format!("failed to write config: {}", config_path.display()))?;

    Ok(())
}

pub fn write_template_config(config_path: &Path) -> Result<()> {
    if config_path.exists() {
        return Ok(());
    }

    if let Some(parent) = config_path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("failed to create config dir: {}", parent.display()))?;
    }

    let template = r#"# slurm-dash configuration
# Docs: https://github.com/haaaaron/slurm-dash

[general]
# max_download_mb = 500

# Add one [servers.<alias>] block per HPC cluster.
# Run `slurm-dash` after editing — it will auto-install the interceptor.
#
# [servers.mycluster]
# ssh_string = "user@login.cluster.edu"
"#;

    std::fs::write(config_path, template)
        .with_context(|| format!("failed to write config template: {}", config_path.display()))?;

    Ok(())
}
