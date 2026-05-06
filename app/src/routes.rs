//! Axum router. All endpoints return either a full page or an HTMX partial.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::sse::{Event as SseEvent, KeepAlive, Sse};
use axum::response::{Html, IntoResponse};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use futures::stream::Stream;
use serde::Deserialize;
use shell_escape::unix::escape;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::StreamExt;

use crate::db;
use crate::jobs::{add_tags, compute_usage, delete_jobs, prepare_rows, remove_tag};
use crate::models::{Progress, SqueueRow};
use crate::slurm::{
    fmt_duration, get_live_status, parse_gpus_from_gres, parse_slurm_time, state_class,
};
use crate::snapshot::{build_file_tree, expand_env, list_snapshot, read_snapshot_file};
use crate::ssh::run_ssh;
use crate::state::{AppState, Event};
use crate::sync::sync_server;
use crate::views;

pub fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/", get(dashboard))
        .route("/jobs/:alias", get(jobs_table))
        .route("/sync/:alias", post(sync_alias))
        .route("/jobs/:alias/:job_id", delete(delete_job_route))
        .route("/jobs/:alias/delete-multiple", post(delete_multiple))
        .route("/jobs/:alias/tag-multiple", post(tag_multiple))
        .route("/jobs/:alias/:job_id/tags/:tag_name", delete(delete_tag_route))
        .route("/jobs/:alias/:job_id/files", get(files_modal_route))
        .route("/jobs/:alias/:job_id/snapshot", get(snapshot_list_route))
        .route("/jobs/:alias/:job_id/snapshot/file", get(snapshot_file_route))
        .route("/squeue/:alias", get(squeue_route))
        .route("/events", get(events_sse))
        .with_state(state)
}

// ─────────────────────────── handlers ───────────────────────────

async fn dashboard(State(state): State<Arc<AppState>>) -> Html<String> {
    let aliases = state.server_aliases();
    Html(views::dashboard(&aliases).into_string())
}

#[derive(Debug, Deserialize)]
struct TagQuery {
    tag: Option<String>,
}

async fn jobs_table(
    State(state): State<Arc<AppState>>,
    Path(alias): Path<String>,
    Query(q): Query<TagQuery>,
) -> Result<Html<String>, AppError> {
    let alias_for_db = alias.clone();
    let tag = q.tag.clone();
    let (jobs, tags_by_job, tags_all) = state
        .db
        .with(move |conn| {
            let jobs = db::load_jobs(conn, &alias_for_db, tag.as_deref())?;
            let tags_by_job = db::tags_for_alias(conn, &alias_for_db)?;
            let tags_all = db::list_all_tags(conn, &alias_for_db)?;
            Ok((jobs, tags_by_job, tags_all))
        })
        .await?;
    // Use cached live status from background loop (no synchronous SSH on render).
    let live = state.live.read().await;
    let status_map = live.get(&alias).cloned();
    drop(live);
    let rows = prepare_rows(&jobs, status_map.as_ref(), &tags_by_job);
    let usage = compute_usage(&rows);
    let html =
        views::jobs_table(&alias, &rows, &usage, &tags_all, q.tag.as_deref()).into_string();
    Ok(Html(html))
}

async fn sync_alias(
    State(state): State<Arc<AppState>>,
    Path(alias): Path<String>,
) -> Result<Html<String>, AppError> {
    let ssh_string = state.ssh_for(&alias);
    if let Some(ssh) = ssh_string {
        let _ = sync_server(&state.db, &alias, &ssh).await;
        let alias_for_db = alias.clone();
        let since = state
            .db
            .with(move |conn| db::oldest_unfinished(conn, &alias_for_db))
            .await
            .unwrap_or(None);
        if let Ok(map) = get_live_status(&ssh, since).await {
            if !map.is_empty() {
                let alias_for_db = alias.clone();
                let map_clone = map.clone();
                let _ = state
                    .db
                    .with(move |conn| db::finalize_terminal(conn, &alias_for_db, &map_clone))
                    .await;
            }
            state.live.write().await.insert(alias.clone(), map);
        }
        let _ = state.events.send(Event::JobsUpdated {
            alias: Some(alias.clone()),
        });
    }
    jobs_table(State(state), Path(alias), Query(TagQuery { tag: None })).await
}

async fn delete_job_route(
    State(state): State<Arc<AppState>>,
    Path((alias, job_id)): Path<(String, String)>,
) -> Result<Html<String>, AppError> {
    delete_jobs(&state, &alias, &[job_id]).await?;
    let _ = state.events.send(Event::JobsUpdated {
        alias: Some(alias.clone()),
    });
    Ok(Html(String::new()))
}

#[derive(Debug, Deserialize)]
struct JobIdsBody {
    job_ids: Vec<String>,
}

async fn delete_multiple(
    State(state): State<Arc<AppState>>,
    Path(alias): Path<String>,
    Json(body): Json<JobIdsBody>,
) -> Result<Html<String>, AppError> {
    if !body.job_ids.is_empty() {
        delete_jobs(&state, &alias, &body.job_ids).await?;
        let _ = state.events.send(Event::JobsUpdated {
            alias: Some(alias.clone()),
        });
    }
    Ok(Html(String::new()))
}

#[derive(Debug, Deserialize)]
struct TagBody {
    job_ids: Vec<String>,
    tag_name: String,
}

async fn tag_multiple(
    State(state): State<Arc<AppState>>,
    Path(alias): Path<String>,
    Json(body): Json<TagBody>,
) -> Result<Html<String>, AppError> {
    if !body.job_ids.is_empty() && !body.tag_name.trim().is_empty() {
        add_tags(&state, &alias, &body.job_ids, &body.tag_name).await?;
        let _ = state.events.send(Event::JobsUpdated {
            alias: Some(alias.clone()),
        });
    }
    Ok(Html(String::new()))
}

async fn delete_tag_route(
    State(state): State<Arc<AppState>>,
    Path((alias, job_id, tag_name)): Path<(String, String, String)>,
) -> Result<Html<String>, AppError> {
    remove_tag(&state.db, &alias, &job_id, &tag_name).await?;
    let _ = state.events.send(Event::JobsUpdated {
        alias: Some(alias.clone()),
    });
    Ok(Html(String::new()))
}

// ─────────────────── files / snapshot ───────────────────

#[derive(Debug)]
struct JobMeta {
    submit_argv: Option<String>,
    #[allow(dead_code)]
    submit_script: Option<String>,
    work_dir: Option<String>,
    snapshot_path: Option<String>,
    env_vars: Option<String>,
}

async fn fetch_job_meta(state: &Arc<AppState>, alias: &str, job_id: &str) -> Result<Option<JobMeta>> {
    let alias = alias.to_string();
    let job_id = job_id.to_string();
    state
        .db
        .with(move |conn| {
            let mut stmt = conn.prepare(
                "SELECT submit_argv, submit_script, work_dir, snapshot_path, env_vars \
                 FROM jobs WHERE server_alias = ? AND job_id = ?",
            )?;
            let row = stmt
                .query_row(rusqlite::params![alias, job_id], |r| {
                    Ok(JobMeta {
                        submit_argv: r.get(0)?,
                        submit_script: r.get(1)?,
                        work_dir: r.get(2)?,
                        snapshot_path: r.get(3)?,
                        env_vars: r.get(4)?,
                    })
                })
                .ok();
            Ok(row)
        })
        .await
}

async fn files_modal_route(
    State(state): State<Arc<AppState>>,
    Path((alias, job_id)): Path<(String, String)>,
) -> Result<Html<String>, AppError> {
    let meta = fetch_job_meta(&state, &alias, &job_id).await?;
    let (submit_cmd, work_dir) = match meta {
        Some(m) => {
            let argv: Vec<String> = m
                .submit_argv
                .as_deref()
                .and_then(|s| serde_json::from_str(s).ok())
                .unwrap_or_default();
            let cmd = if argv.is_empty() {
                String::new()
            } else {
                let joined: Vec<String> = argv
                    .iter()
                    .map(|a| escape(a.into()).into_owned())
                    .collect();
                format!("sbatch {}", joined.join(" "))
            };
            (cmd, m.work_dir.unwrap_or_default())
        }
        None => (String::new(), String::new()),
    };
    Ok(Html(
        views::files_modal(&alias, &job_id, &submit_cmd, &work_dir).into_string(),
    ))
}

async fn snapshot_list_route(
    State(state): State<Arc<AppState>>,
    Path((alias, job_id)): Path<(String, String)>,
) -> Result<Html<String>, AppError> {
    let ssh = state.ssh_for(&alias);
    let meta = fetch_job_meta(&state, &alias, &job_id).await?;
    let snapshot_path = meta.as_ref().and_then(|m| m.snapshot_path.clone()).unwrap_or_default();

    let mut tree = Vec::new();
    let mut error: Option<String> = None;

    if snapshot_path.is_empty() {
        error = Some("No snapshot available for this job.".into());
    } else if ssh.is_none() {
        error = Some("No SSH connection configured.".into());
    } else if let Some(ssh) = ssh {
        match list_snapshot(&ssh, &snapshot_path).await {
            Ok(out) if out.success => {
                let paths: Vec<String> =
                    out.stdout.lines().filter(|l| !l.is_empty()).map(|s| s.to_string()).collect();
                tree = build_file_tree(&paths);
            }
            Ok(out) => {
                let stderr = out.stderr.trim();
                error = Some(if stderr.is_empty() { "tar failed".into() } else { stderr.into() });
            }
            Err(e) => error = Some(e.to_string()),
        }
    }

    Ok(Html(views::snapshot_file_list(&tree, error.as_deref()).into_string()))
}

#[derive(Debug, Deserialize)]
struct SnapshotFileQuery {
    path: Option<String>,
    #[serde(default)]
    expand_env: u8,
}

async fn snapshot_file_route(
    State(state): State<Arc<AppState>>,
    Path((alias, job_id)): Path<(String, String)>,
    Query(q): Query<SnapshotFileQuery>,
) -> Result<Html<String>, AppError> {
    let path = q.path.unwrap_or_default();
    if path.is_empty() {
        return Ok(Html(
            "<p class='text-gray-500 italic p-4'>Select a file to preview.</p>".into(),
        ));
    }
    let ssh = state.ssh_for(&alias);
    let meta = fetch_job_meta(&state, &alias, &job_id).await?;

    let mut content = String::new();
    let mut error: Option<String> = None;
    if let (Some(ssh), Some(m)) = (ssh, meta) {
        if let Some(snap) = m.snapshot_path.as_deref().filter(|s| !s.is_empty()) {
            match read_snapshot_file(&ssh, snap, &path).await {
                Ok(out) if out.success => {
                    let raw = out.stdout;
                    let head: &str = raw.get(..4096.min(raw.len())).unwrap_or("");
                    if head.contains('\u{0}') {
                        content = "(binary file — preview not available)".into();
                    } else if q.expand_env == 1 {
                        if let Some(env_str) = m.env_vars.as_deref() {
                            if let Ok(env) = serde_json::from_str::<serde_json::Value>(env_str) {
                                content = expand_env(&raw, &env);
                            } else {
                                content = raw;
                            }
                        } else {
                            content = raw;
                        }
                    } else {
                        content = raw;
                    }
                }
                Ok(out) => {
                    error = Some(
                        if out.stderr.trim().is_empty() {
                            "Failed to extract file.".into()
                        } else {
                            out.stderr.trim().to_string()
                        },
                    );
                }
                Err(e) => error = Some(e.to_string()),
            }
        } else {
            error = Some("Snapshot not available.".into());
        }
    } else {
        error = Some("Snapshot not available.".into());
    }
    Ok(Html(views::snapshot_preview(&content, error.as_deref()).into_string()))
}

// ─────────────────── squeue modal ───────────────────

async fn squeue_route(
    State(state): State<Arc<AppState>>,
    Path(alias): Path<String>,
) -> Result<Html<String>, AppError> {
    let ssh = state.ssh_for(&alias);
    let mut rows: Vec<SqueueRow> = Vec::new();
    let mut error: Option<String> = None;

    if let Some(ssh) = ssh {
        let cmd = "squeue --me --noheader --format='%i|%T|%P|%j|%M|%l|%R|%C|%b'";
        match run_ssh(&ssh, cmd, Duration::from_secs(12)).await {
            Ok(out) if out.success => {
                for line in out.stdout.trim().lines() {
                    let fields: Vec<&str> = line.split('|').collect();
                    if fields.len() < 9 {
                        continue;
                    }
                    let jid = fields[0].to_string();
                    let state_str = fields[1].to_string();
                    let partition = fields[2].to_string();
                    let name = fields[3].to_string();
                    let t_used = fields[4].to_string();
                    let t_limit = fields[5].to_string();
                    let node_reason = fields[6].to_string();
                    let cpus = fields[7].to_string();
                    let gres = fields[8];
                    let used_s = parse_slurm_time(&t_used);
                    let limit_s = parse_slurm_time(&t_limit);
                    let (gpus, _gpu_model) = parse_gpus_from_gres(gres);
                    let progress = if state_str == "RUNNING" {
                        if let (Some(u), Some(l)) = (used_s, limit_s) {
                            if l > 0 {
                                let pct = ((u as f64 * 100.0) / (l as f64)).min(100.0) as u32;
                                let remaining = (l - u).max(0);
                                Some(Progress {
                                    pct,
                                    remaining: fmt_duration(remaining),
                                    color: if pct >= 90 {
                                        "bg-red-500"
                                    } else if pct >= 75 {
                                        "bg-yellow-400"
                                    } else {
                                        "bg-green-500"
                                    },
                                })
                            } else {
                                None
                            }
                        } else {
                            None
                        }
                    } else {
                        None
                    };
                    rows.push(SqueueRow {
                        job_id: jid,
                        state_class: state_class(&state_str).to_string(),
                        state: state_str,
                        partition,
                        name,
                        time_used: t_used,
                        time_limit: t_limit,
                        node_reason,
                        cpus,
                        gpus,
                        progress,
                    });
                }
            }
            Ok(out) => {
                error = Some(
                    if out.stderr.trim().is_empty() {
                        "squeue failed".into()
                    } else {
                        out.stderr.trim().to_string()
                    },
                );
            }
            Err(e) => error = Some(e.to_string()),
        }
    } else {
        error = Some("No SSH connection configured.".into());
    }

    rows.sort_by(|a, b| {
        let key = |r: &SqueueRow| (r.state != "RUNNING", r.state.clone(), r.job_id.clone());
        key(a).cmp(&key(b))
    });
    let running = rows.iter().filter(|r| r.state == "RUNNING").count() as u64;
    let pending = rows.iter().filter(|r| r.state == "PENDING").count() as u64;
    let total = rows.len() as u64;

    Ok(Html(
        views::squeue_modal(&alias, &rows, error.as_deref(), running, pending, total)
            .into_string(),
    ))
}

// ─────────────────── SSE ───────────────────

async fn events_sse(
    State(state): State<Arc<AppState>>,
) -> Sse<impl Stream<Item = std::result::Result<SseEvent, Infallible>>> {
    let rx = state.events.subscribe();
    let stream = BroadcastStream::new(rx).filter_map(|res| match res {
        Ok(Event::JobsUpdated { alias }) => {
            // htmx-sse listens for events with a given name. We emit a per-alias
            // event so panels only refetch when their data changes.
            let name = match alias {
                Some(a) => format!("jobs-{a}"),
                None => "jobs".into(),
            };
            Some(Ok(SseEvent::default().event(name).data("update")))
        }
        Err(_) => None,
    });
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

// ─────────────────── error glue ───────────────────

pub struct AppError(anyhow::Error);

impl<E: Into<anyhow::Error>> From<E> for AppError {
    fn from(e: E) -> Self {
        AppError(e.into())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> axum::response::Response {
        tracing::error!(error = ?self.0, "request failed");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("internal error: {}", self.0),
        )
            .into_response()
    }
}

