//! Domain types shared across modules.

use serde::{Deserialize, Serialize};

/// Raw row pulled from `jobs` table (selected columns only).
#[derive(Debug, Clone, Default)]
pub struct JobRow {
    pub job_id: String,
    pub array_base_id: Option<String>,
    pub submit_time: Option<f64>,
    pub work_dir: Option<String>,
    pub git_hash: Option<String>,
    pub snapshot_path: Option<String>,
    pub submit_argv: Option<String>,
    pub env_vars: Option<String>,
    pub final_state: Option<String>,
    pub final_cpus: Option<String>,
    pub final_req_mem: Option<String>,
    pub final_max_rss: Option<String>,
    pub final_gpus: Option<i64>,
    pub final_gpu_model: Option<String>,
    pub final_node_list: Option<String>,
}

/// Live state info from squeue/sacct.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LiveStatus {
    pub state: String,
    pub cpus: String,
    pub req_mem: String,
    #[serde(default)]
    pub gpus: i64,
    #[serde(default)]
    pub gpu_model: String,
    pub node_list: String,
}

/// Display-ready row passed to views.
#[derive(Debug, Clone)]
pub struct DisplayRow {
    pub job_id: String,
    pub job_name: String,
    pub state: String,
    pub state_class: String,
    pub submit_time: String,
    pub work_dir: String,
    pub git_hash: String,
    pub cpus: String,
    pub req_mem: String,
    pub gpus: i64,
    pub gpu_model: String,
    pub node_list: String,
    pub snapshot_path: String,
    pub submit_cmd: String,
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct Usage {
    pub running: u64,
    pub pending: u64,
    pub total: u64,
    pub cpus: u64,
    pub gpus: u64,
    pub mem_gb: f64,
}

#[derive(Debug, Clone)]
pub struct SqueueRow {
    pub job_id: String,
    pub state: String,
    pub state_class: String,
    pub partition: String,
    pub name: String,
    pub time_used: String,
    pub time_limit: String,
    pub node_reason: String,
    pub cpus: String,
    pub gpus: i64,
    pub progress: Option<Progress>,
}

#[derive(Debug, Clone)]
pub struct Progress {
    pub pct: u32,
    pub remaining: String,
    pub color: &'static str,
}

#[derive(Debug, Clone)]
pub struct TreeNode {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
    pub is_submit_script: bool,
    pub children: Vec<TreeNode>,
}
