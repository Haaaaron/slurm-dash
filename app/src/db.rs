//! SQLite layer. Uses rusqlite synchronously inside `spawn_blocking` so axum
//! handlers stay async without pulling in an async-sqlite stack.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use tokio::sync::Mutex;

use crate::models::JobRow;

/// Thread-safe wrapper around a single SQLite connection. SQLite handles
/// internal locking; the Mutex serialises rusqlite calls (which are not Sync).
#[derive(Clone)]
pub struct Db {
    conn: Arc<Mutex<Connection>>,
}

impl Db {
    pub async fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        let path_owned = path.to_path_buf();
        let conn = tokio::task::spawn_blocking(move || -> Result<Connection> {
            let conn = Connection::open(&path_owned)
                .with_context(|| format!("open sqlite {}", path_owned.display()))?;
            conn.busy_timeout(std::time::Duration::from_secs(60))?;
            init_schema(&conn)?;
            Ok(conn)
        })
        .await??;
        Ok(Self { conn: Arc::new(Mutex::new(conn)) })
    }

    /// Run a blocking closure with the connection.
    pub async fn with<F, R>(&self, f: F) -> Result<R>
    where
        F: FnOnce(&mut Connection) -> Result<R> + Send + 'static,
        R: Send + 'static,
    {
        let conn = self.conn.clone();
        tokio::task::spawn_blocking(move || {
            let mut guard = conn.blocking_lock();
            f(&mut guard)
        })
        .await?
    }
}

/// Mirror of the Python schema in `config.py:_init_db`.
fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS jobs (
            server_alias TEXT,
            job_id TEXT,
            array_base_id TEXT,
            array_task_id TEXT,
            submit_time REAL,
            work_dir TEXT,
            snapshot_path TEXT,
            git_hash TEXT,
            git_diff TEXT,
            submit_script TEXT,
            submit_argv TEXT,
            env_vars TEXT,
            is_deleted INTEGER DEFAULT 0,
            updated_at REAL,
            final_state TEXT,
            final_cpus TEXT,
            final_req_mem TEXT,
            final_max_rss TEXT,
            final_gpus INTEGER,
            final_gpu_model TEXT,
            final_node_list TEXT,
            output_probed_at REAL,
            PRIMARY KEY (server_alias, job_id)
        );
        CREATE TABLE IF NOT EXISTS outputs (
            server_alias TEXT,
            job_id TEXT,
            kind TEXT,
            path TEXT,
            size_bytes INTEGER,
            mtime REAL,
            exists_remote INTEGER DEFAULT 0,
            is_dir INTEGER DEFAULT 0,
            head_text TEXT,
            local_path TEXT,
            discovered_at REAL,
            probed_at REAL,
            PRIMARY KEY (server_alias, job_id, path)
        );
        CREATE TABLE IF NOT EXISTS tags (
            server_alias TEXT,
            job_id TEXT,
            tag_name TEXT,
            PRIMARY KEY (server_alias, job_id, tag_name)
        );
        "#,
    )?;
    // Best-effort migrations (mirrors Python).
    let extras: &[(&str, &str)] = &[
        ("submit_script", "TEXT"),
        ("submit_argv", "TEXT"),
        ("env_vars", "TEXT"),
        ("final_state", "TEXT"),
        ("final_cpus", "TEXT"),
        ("final_req_mem", "TEXT"),
        ("final_max_rss", "TEXT"),
        ("final_gpus", "INTEGER"),
        ("final_gpu_model", "TEXT"),
        ("final_node_list", "TEXT"),
        ("output_probed_at", "REAL"),
    ];
    for (col, ty) in extras {
        let _ = conn.execute(&format!("ALTER TABLE jobs ADD COLUMN {col} {ty}"), []);
    }
    let _ = conn.execute("ALTER TABLE outputs ADD COLUMN local_path TEXT", []);
    Ok(())
}

/// Load all non-deleted jobs for an alias, optionally filtered by tag.
pub fn load_jobs(
    conn: &Connection,
    alias: &str,
    tag_filter: Option<&str>,
) -> Result<Vec<JobRow>> {
    let cols = "job_id, array_base_id, submit_time, work_dir, git_hash, snapshot_path, \
                submit_argv, env_vars, final_state, final_cpus, final_req_mem, final_max_rss, \
                final_gpus, final_gpu_model, final_node_list";
    let rows: Vec<JobRow> = match tag_filter {
        Some(tag) => {
            let sql = format!(
                "SELECT {cols} FROM jobs j \
                 INNER JOIN tags t ON j.server_alias = t.server_alias AND j.job_id = t.job_id \
                 WHERE j.server_alias = ? AND j.is_deleted = 0 AND t.tag_name = ? \
                 ORDER BY j.submit_time DESC"
            );
            let mut stmt = conn.prepare(&sql)?;
            let mapped = stmt
                .query_map(params![alias, tag], row_to_job)?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            mapped
        }
        None => {
            let sql = format!(
                "SELECT {cols} FROM jobs WHERE server_alias = ? AND is_deleted = 0 \
                 ORDER BY submit_time DESC"
            );
            let mut stmt = conn.prepare(&sql)?;
            let mapped = stmt
                .query_map(params![alias], row_to_job)?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            mapped
        }
    };
    Ok(rows)
}

fn row_to_job(r: &rusqlite::Row<'_>) -> rusqlite::Result<JobRow> {
    Ok(JobRow {
        job_id: r.get(0)?,
        array_base_id: r.get(1)?,
        submit_time: r.get(2)?,
        work_dir: r.get(3)?,
        git_hash: r.get(4)?,
        snapshot_path: r.get(5)?,
        submit_argv: r.get(6)?,
        env_vars: r.get(7)?,
        final_state: r.get(8)?,
        final_cpus: r.get(9)?,
        final_req_mem: r.get(10)?,
        final_max_rss: r.get(11)?,
        final_gpus: r.get(12)?,
        final_gpu_model: r.get(13)?,
        final_node_list: r.get(14)?,
    })
}

pub fn oldest_unfinished(conn: &Connection, alias: &str) -> Result<Option<f64>> {
    let v: Option<f64> = conn
        .query_row(
            "SELECT MIN(submit_time) FROM jobs WHERE server_alias = ? AND is_deleted = 0 \
             AND (final_state IS NULL OR final_state = '')",
            params![alias],
            |r| r.get::<_, Option<f64>>(0),
        )
        .unwrap_or(None);
    Ok(v)
}

pub fn tags_for_alias(
    conn: &Connection,
    alias: &str,
) -> Result<std::collections::HashMap<String, Vec<String>>> {
    let mut out: std::collections::HashMap<String, Vec<String>> =
        std::collections::HashMap::new();
    let mut stmt = conn.prepare(
        "SELECT job_id, tag_name FROM tags WHERE server_alias = ? ORDER BY job_id, tag_name",
    )?;
    for row in stmt.query_map(params![alias], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
    })? {
        let (jid, tag) = row?;
        out.entry(jid).or_default().push(tag);
    }
    Ok(out)
}

pub fn list_all_tags(conn: &Connection, alias: &str) -> Result<Vec<String>> {
    let mut stmt = conn.prepare(
        "SELECT DISTINCT tag_name FROM tags WHERE server_alias = ? ORDER BY tag_name",
    )?;
    let tags = stmt
        .query_map(params![alias], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(tags)
}

pub fn finalize_terminal(
    conn: &mut Connection,
    alias: &str,
    status_map: &std::collections::HashMap<String, crate::models::LiveStatus>,
) -> Result<()> {
    let tx = conn.transaction()?;
    for (job_id, st) in status_map {
        if !crate::slurm::is_terminal(&st.state) {
            continue;
        }
        tx.execute(
            "UPDATE jobs SET final_state=?, final_cpus=?, final_req_mem=?, \
             final_max_rss=NULL, final_gpus=?, final_gpu_model=?, final_node_list=? \
             WHERE server_alias=? AND job_id=? AND (final_state IS NULL OR final_state='')",
            params![
                st.state,
                st.cpus,
                st.req_mem,
                st.gpus,
                st.gpu_model,
                st.node_list,
                alias,
                job_id
            ],
        )?;
    }
    tx.commit()?;
    Ok(())
}

pub fn purge_alias(conn: &Connection, alias: &str) -> Result<usize> {
    conn.execute("DELETE FROM outputs WHERE server_alias = ?", params![alias])?;
    conn.execute("DELETE FROM tags WHERE server_alias = ?", params![alias])?;
    let count = conn.execute("DELETE FROM jobs WHERE server_alias = ?", params![alias])?;
    Ok(count)
}
