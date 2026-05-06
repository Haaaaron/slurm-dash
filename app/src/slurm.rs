//! squeue/sacct parsing + GPU/time helpers. Mirrors `slurm_api.py`.

use std::collections::HashMap;
use std::time::Duration;

use anyhow::Result;
use chrono::{DateTime, Local, TimeZone};
use once_cell::sync::Lazy;
use regex::Regex;

use crate::models::LiveStatus;
use crate::ssh::run_ssh;

static GPU_TRES_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"gres/gpu(?::([^=,]+))?=(\d+)").unwrap());
static GPU_GRES_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"gpu(?::([^:=,(]+))?[:=](\d+)").unwrap());

const TERMINAL_STATES: &[&str] = &[
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL",
    "DEADLINE", "OUT_OF_MEMORY", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
];

pub fn is_terminal(state: &str) -> bool {
    if state.is_empty() {
        return false;
    }
    let head = state
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_ascii_uppercase();
    TERMINAL_STATES.iter().any(|s| *s == head)
}

pub fn state_class(state: &str) -> &'static str {
    let head = state
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_ascii_uppercase();
    match head.as_str() {
        "RUNNING" => "bg-green-950 text-green-300",
        "PENDING" => "bg-yellow-950 text-yellow-300",
        "COMPLETED" => "bg-gray-800 text-gray-400",
        "FAILED" | "CANCELLED" => "bg-red-950 text-red-300",
        "COMPLETING" => "bg-cyan-950 text-cyan-300",
        "TIMEOUT" => "bg-orange-950 text-orange-300",
        "PREEMPTED" => "bg-purple-950 text-purple-300",
        _ => "bg-gray-800 text-gray-400",
    }
}

pub fn parse_gpus_from_tres(tres: &str) -> (i64, String) {
    if tres.is_empty() {
        return (0, String::new());
    }
    let mut models = std::collections::BTreeSet::new();
    let mut total: i64 = 0;
    for cap in GPU_TRES_RE.captures_iter(tres) {
        let model = cap.get(1).map(|m| m.as_str().to_uppercase());
        let count: i64 = cap.get(2).map_or(0, |m| m.as_str().parse().unwrap_or(0));
        if let Some(m) = model {
            models.insert(m);
        }
        total += count;
    }
    let joined = models.into_iter().collect::<Vec<_>>().join("/");
    (total, joined)
}

pub fn parse_gpus_from_gres(gres: &str) -> (i64, String) {
    if gres.is_empty() || gres == "(null)" || gres == "N/A" {
        return (0, String::new());
    }
    let mut models = std::collections::BTreeSet::new();
    let mut total: i64 = 0;
    for cap in GPU_GRES_RE.captures_iter(gres) {
        let model = cap.get(1).map(|m| m.as_str().to_uppercase());
        let count: i64 = cap.get(2).map_or(0, |m| m.as_str().parse().unwrap_or(0));
        if let Some(m) = model {
            models.insert(m);
        }
        total += count;
    }
    let joined = models.into_iter().collect::<Vec<_>>().join("/");
    (total, joined)
}

pub fn parse_slurm_time(s: &str) -> Option<i64> {
    let s = s.trim();
    if s.is_empty()
        || matches!(
            s,
            "UNLIMITED" | "INVALID" | "NOT_SET" | "N/A" | "Partition_Limit"
        )
    {
        return None;
    }
    let (days, rest) = if let Some((d, r)) = s.split_once('-') {
        (d.parse::<i64>().ok()?, r)
    } else {
        (0, s)
    };
    let parts: Vec<&str> = rest.split(':').collect();
    let nums: Vec<i64> = parts.iter().map(|p| p.parse().ok()).collect::<Option<_>>()?;
    let (h, m, sec) = match nums.len() {
        3 => (nums[0], nums[1], nums[2]),
        2 => (0, nums[0], nums[1]),
        1 => (0, 0, nums[0]),
        _ => return None,
    };
    Some(days * 86400 + h * 3600 + m * 60 + sec)
}

pub fn fmt_duration(secs: i64) -> String {
    let secs = secs.max(0);
    let d = secs / 86400;
    let rem = secs % 86400;
    let h = rem / 3600;
    let m = (rem % 3600) / 60;
    if d > 0 {
        format!("{d}d {h}h")
    } else if h > 0 {
        format!("{h}h {m:02}m")
    } else {
        format!("{m}m")
    }
}

pub fn fmt_dt(ts: f64) -> String {
    if ts <= 0.0 {
        return String::new();
    }
    let secs = ts as i64;
    let dt: DateTime<Local> = Local
        .timestamp_opt(secs, 0)
        .single()
        .unwrap_or_else(Local::now);
    dt.format("%Y-%m-%d %H:%M").to_string()
}

pub fn parse_mem_gb(s: &str) -> f64 {
    let s = s.trim();
    if s.is_empty() || matches!(s, "N/A" | "(null)") {
        return 0.0;
    }
    let mut s = s.to_string();
    if let Some(last) = s.chars().last() {
        if last.eq_ignore_ascii_case(&'c') || last.eq_ignore_ascii_case(&'n') {
            s.pop();
        }
    }
    let mul = match s.chars().last() {
        Some('k') | Some('K') => Some(1.0 / 1e6),
        Some('m') | Some('M') => Some(1.0 / 1e3),
        Some('g') | Some('G') => Some(1.0),
        Some('t') | Some('T') => Some(1024.0),
        _ => None,
    };
    if let Some(m) = mul {
        s.pop();
        s.parse::<f64>().map(|v| v * m).unwrap_or(0.0)
    } else {
        s.parse::<f64>().map(|v| v / 1e9).unwrap_or(0.0)
    }
}

pub async fn get_live_status(
    ssh_string: &str,
    since: Option<f64>,
) -> Result<HashMap<String, LiveStatus>> {
    let now = chrono::Local::now().timestamp() as f64;
    let since = since.unwrap_or(now - 86400.0);
    let since_secs = (since - 86400.0).max(0.0) as i64;
    let since_dt = chrono::Local
        .timestamp_opt(since_secs, 0)
        .single()
        .unwrap_or_else(chrono::Local::now);
    let since_str = since_dt.format("%Y-%m-%d").to_string();

    let cmd = format!(
        "squeue -u $USER --noheader --format=\"%i|%T|%b|%R\" 2>/dev/null || true; \
         echo \"---\"; \
         sacct -X --parsable2 -S {since_str} --format=\"JobID,State,AllocCPUs,ReqMem,AllocTRES,NodeList\" 2>/dev/null || true"
    );
    let out = run_ssh(ssh_string, &cmd, Duration::from_secs(15)).await?;
    if !out.success {
        return Ok(HashMap::new());
    }
    let stdout = out.stdout.trim().to_string();
    let parts: Vec<&str> = stdout.splitn(2, "---").collect();
    let squeue_out = parts.first().map(|s| s.trim()).unwrap_or("");
    let sacct_out = parts.get(1).map(|s| s.trim()).unwrap_or("");

    let mut map: HashMap<String, LiveStatus> = HashMap::new();

    for line in sacct_out.lines() {
        if line.trim().is_empty() || line.starts_with("JobID|") {
            continue;
        }
        let cols: Vec<&str> = line.split('|').collect();
        if cols.len() < 4 {
            continue;
        }
        let job_id = cols[0].split('_').next().unwrap_or(cols[0]).to_string();
        let state = cols[1].to_string();
        let cpus = cols[2].to_string();
        let req_mem = cols[3].to_string();
        let tres = cols.get(4).copied().unwrap_or("");
        let node_list = cols.get(5).copied().unwrap_or("").to_string();
        let (gpus, gpu_model) = parse_gpus_from_tres(tres);
        map.insert(
            job_id,
            LiveStatus {
                state,
                cpus,
                req_mem,
                gpus,
                gpu_model,
                node_list,
            },
        );
    }

    for line in squeue_out.lines() {
        if line.trim().is_empty() || line.starts_with("JobID|") {
            continue;
        }
        let cols: Vec<&str> = line.split('|').collect();
        if cols.len() < 2 {
            continue;
        }
        let job_id = cols[0].split('_').next().unwrap_or(cols[0]).to_string();
        let state = cols[1].to_string();
        let gres = cols.get(2).copied().unwrap_or("");
        let node_or_reason = cols.get(3).copied().unwrap_or("").to_string();
        let (gpus, gpu_model) = parse_gpus_from_gres(gres);
        match map.get_mut(&job_id) {
            Some(entry) => {
                entry.state = state;
                if entry.gpus == 0 {
                    entry.gpus = gpus;
                    entry.gpu_model = gpu_model;
                }
                if !node_or_reason.is_empty()
                    && node_or_reason != "(null)"
                    && node_or_reason != "N/A"
                {
                    entry.node_list = node_or_reason;
                }
            }
            None => {
                map.insert(
                    job_id,
                    LiveStatus {
                        state,
                        cpus: "N/A".into(),
                        req_mem: "N/A".into(),
                        gpus,
                        gpu_model,
                        node_list: node_or_reason,
                    },
                );
            }
        }
    }
    Ok(map)
}
