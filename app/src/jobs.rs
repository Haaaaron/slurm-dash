//! Soft-delete + tag CRUD + display row prep.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use chrono::Local;
use rusqlite::params;
use shell_escape::unix::escape;

use crate::db::Db;
use crate::models::{DisplayRow, JobRow, LiveStatus, Usage};
use crate::ssh::run_ssh;
use crate::state::AppState;

const REMOTE_DELETE_SCRIPT: &str = r#"import os, sqlite3, sys
job_id = sys.argv[1]
updated_at = float(sys.argv[2])
db = os.path.expanduser('~/.slurm_tracker/.slurm_tracker.db')
if not os.path.exists(db):
    db = os.path.expanduser('~/.slurm_tracker.db')
if os.path.exists(db):
    c = sqlite3.connect(db, timeout=60)
    row = c.execute('SELECT snapshot_path FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
    c.execute('UPDATE jobs SET is_deleted = 1, updated_at = ? WHERE job_id = ?', (updated_at, job_id))
    c.commit()
    c.close()
    if row and row[0]:
        try: os.remove(row[0])
        except OSError: pass
"#;

pub async fn delete_jobs(state: &Arc<AppState>, alias: &str, job_ids: &[String]) -> Result<()> {
    let now = Local::now().timestamp() as f64;
    let alias_owned = alias.to_string();
    let ids_owned = job_ids.to_vec();
    state
        .db
        .with(move |conn| {
            let tx = conn.transaction()?;
            for jid in &ids_owned {
                tx.execute(
                    "UPDATE jobs SET is_deleted = 1, updated_at = ? \
                     WHERE server_alias = ? AND job_id = ?",
                    params![now, alias_owned, jid],
                )?;
                tx.execute(
                    "DELETE FROM outputs WHERE server_alias = ? AND job_id = ?",
                    params![alias_owned, jid],
                )?;
            }
            tx.commit()?;
            Ok(())
        })
        .await?;

    // Fire-and-forget remote deletion.
    if let Some(server) = state.config.servers.get(alias) {
        if let Some(ssh_string) = server.ssh_string.clone() {
            let ids_for_remote = job_ids.to_vec();
            tokio::spawn(async move {
                for jid in ids_for_remote {
                    let quoted_script = escape(REMOTE_DELETE_SCRIPT.into());
                    let quoted_jid = escape(jid.clone().into());
                    let cmd = format!(
                        "python3 -c {quoted_script} {quoted_jid} {now}"
                    );
                    let _ = run_ssh(&ssh_string, &cmd, Duration::from_secs(30)).await;
                }
            });
        }
    }
    Ok(())
}

pub async fn add_tags(state: &Arc<AppState>, alias: &str, job_ids: &[String], tag: &str) -> Result<()> {
    let tag = tag.trim().to_string();
    if tag.is_empty() {
        return Ok(());
    }
    let alias_owned = alias.to_string();
    let ids_owned = job_ids.to_vec();
    state
        .db
        .with(move |conn| {
            let tx = conn.transaction()?;
            for jid in &ids_owned {
                tx.execute(
                    "INSERT OR IGNORE INTO tags (server_alias, job_id, tag_name) VALUES (?,?,?)",
                    params![alias_owned, jid, tag],
                )?;
            }
            tx.commit()?;
            Ok(())
        })
        .await?;
    Ok(())
}

pub async fn remove_tag(db: &Db, alias: &str, job_id: &str, tag: &str) -> Result<()> {
    let alias_owned = alias.to_string();
    let job_id = job_id.to_string();
    let tag = tag.to_string();
    db.with(move |conn| {
        conn.execute(
            "DELETE FROM tags WHERE server_alias = ? AND job_id = ? AND tag_name = ?",
            params![alias_owned, job_id, tag],
        )?;
        Ok(())
    })
    .await
}

/// Translate `JobRow` + optional live status into a display-ready row.
pub fn prepare_rows(
    jobs: &[JobRow],
    status_map: Option<&HashMap<String, LiveStatus>>,
    tags_by_job: &HashMap<String, Vec<String>>,
) -> Vec<DisplayRow> {
    let mut out = Vec::with_capacity(jobs.len());
    for job in jobs {
        let live = status_map.and_then(|m| m.get(&job.job_id));
        let (state, cpus, req_mem, gpus, gpu_model, node_list) =
            if let Some(fs) = job.final_state.as_deref().filter(|s| !s.is_empty()) {
                (
                    fs.to_string(),
                    job.final_cpus.clone().unwrap_or_default(),
                    job.final_req_mem.clone().unwrap_or_default(),
                    job.final_gpus.unwrap_or(0),
                    job.final_gpu_model.clone().unwrap_or_default(),
                    job.final_node_list.clone().unwrap_or_default(),
                )
            } else if let Some(l) = live {
                (
                    l.state.clone(),
                    l.cpus.clone(),
                    l.req_mem.clone(),
                    l.gpus,
                    l.gpu_model.clone(),
                    l.node_list.clone(),
                )
            } else {
                let placeholder = if status_map.is_none() { "" } else { "COMPLETED" };
                (
                    placeholder.into(),
                    String::new(),
                    String::new(),
                    0,
                    String::new(),
                    String::new(),
                )
            };

        let argv: Vec<String> = job
            .submit_argv
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();

        let job_name = extract_job_name(&argv);
        let submit_cmd = if argv.is_empty() {
            String::new()
        } else {
            format!("sbatch {}", shell_join(&argv))
        };

        let git_hash = job
            .git_hash
            .as_deref()
            .unwrap_or("")
            .chars()
            .take(7)
            .collect::<String>();

        let tags = tags_by_job.get(&job.job_id).cloned().unwrap_or_default();

        out.push(DisplayRow {
            job_id: job.job_id.clone(),
            job_name,
            state_class: crate::slurm::state_class(&state).to_string(),
            state,
            submit_time: crate::slurm::fmt_dt(job.submit_time.unwrap_or(0.0)),
            work_dir: job.work_dir.clone().unwrap_or_default(),
            git_hash,
            cpus,
            req_mem,
            gpus,
            gpu_model,
            node_list,
            snapshot_path: job.snapshot_path.clone().unwrap_or_default(),
            submit_cmd,
            tags,
        });
    }
    out
}

fn extract_job_name(argv: &[String]) -> String {
    let mut iter = argv.iter().enumerate();
    while let Some((i, arg)) = iter.next() {
        if arg == "--job-name" || arg == "-J" {
            if let Some(next) = argv.get(i + 1) {
                return next.clone();
            }
        }
        if let Some(rest) = arg.strip_prefix("--job-name=") {
            return rest.to_string();
        }
    }
    String::new()
}

fn shell_join(args: &[String]) -> String {
    args.iter()
        .map(|a| escape(a.into()).into_owned())
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn compute_usage(rows: &[DisplayRow]) -> Usage {
    let mut u = Usage::default();
    u.total = rows.len() as u64;
    for r in rows {
        match r.state.as_str() {
            "RUNNING" => {
                u.running += 1;
                u.cpus += r.cpus.parse::<u64>().unwrap_or(0);
                u.gpus += r.gpus.max(0) as u64;
                u.mem_gb += crate::slurm::parse_mem_gb(&r.req_mem);
            }
            "PENDING" => u.pending += 1,
            _ => {}
        }
    }
    u.mem_gb = (u.mem_gb * 10.0).round() / 10.0;
    u
}
