//! Port of `sync_engine.py`. Pulls newer rows from the remote SQLite via SSH
//! using an embedded Python script (the remote side stays Python — no Rust
//! deployed onto the cluster), then UPSERTs into the local DB.

use std::time::Duration;

use anyhow::Result;
use rusqlite::params;

use crate::db::Db;
use crate::ssh::run_ssh;

pub const REMOTE_COLUMNS: [&str; 13] = [
    "job_id", "array_base_id", "array_task_id",
    "submit_time", "work_dir", "snapshot_path",
    "git_hash", "git_diff",
    "submit_script", "submit_argv",
    "env_vars",
    "is_deleted", "updated_at",
];

const REMOTE_SCRIPT: &str = r#"import os, sqlite3, csv, sys
cutoff = float(sys.argv[1])
cols = ['job_id','array_base_id','array_task_id','submit_time','work_dir','snapshot_path','git_hash','git_diff','submit_script','submit_argv','env_vars','is_deleted','updated_at']
optional = ('submit_script','submit_argv','env_vars')
db = os.path.expanduser('~/.slurm_tracker/.slurm_tracker.db')
if not os.path.exists(db):
    db = os.path.expanduser('~/.slurm_tracker.db')
if os.path.exists(db):
    conn = sqlite3.connect(db, timeout=60)
    cur = conn.cursor()
    try:
        cur.execute('SELECT ' + ','.join(cols) + ' FROM jobs WHERE updated_at > ?', (cutoff,))
    except sqlite3.OperationalError:
        fallback = [c for c in cols if c not in optional]
        cur.execute('SELECT ' + ','.join(fallback) + ' FROM jobs WHERE updated_at > ?', (cutoff,))
        rows = []
        for r in cur.fetchall():
            d = dict(zip(fallback, r))
            rows.append([d.get(c, '') for c in cols])
        writer = csv.writer(sys.stdout)
        writer.writerows(rows)
    else:
        writer = csv.writer(sys.stdout)
        writer.writerows(cur.fetchall())
"#;

pub async fn sync_server(db: &Db, alias: &str, ssh_string: &str) -> Result<usize> {
    let alias_owned = alias.to_string();
    let max_updated: f64 = db
        .with(move |conn| {
            let v: Option<f64> = conn
                .query_row(
                    "SELECT MAX(updated_at) FROM jobs WHERE server_alias = ?",
                    params![alias_owned],
                    |r| r.get::<_, Option<f64>>(0),
                )
                .unwrap_or(None);
            Ok(v.unwrap_or(0.0))
        })
        .await?;

    // Build the remote command: python3 -c '<script>' <cutoff>
    let quoted_script = shell_escape::unix::escape(REMOTE_SCRIPT.into());
    let cmd = format!("python3 -c {quoted_script} {max_updated}");
    let out = run_ssh(ssh_string, &cmd, Duration::from_secs(60)).await?;
    if !out.success || out.stdout.trim().is_empty() {
        return Ok(0);
    }

    let alias_owned = alias.to_string();
    let stdout = out.stdout;
    let inserted = db
        .with(move |conn| {
            let tx = conn.transaction()?;
            let mut count = 0usize;
            let mut rdr = csv::ReaderBuilder::new()
                .has_headers(false)
                .flexible(true)
                .from_reader(stdout.as_bytes());
            for record in rdr.records() {
                let row = match record {
                    Ok(r) => r,
                    Err(_) => continue,
                };
                if row.len() < REMOTE_COLUMNS.len() {
                    continue;
                }
                let get = |i: usize| -> &str { row.get(i).unwrap_or("") };
                let job_id = get(0);
                let array_base_id = opt(get(1));
                let array_task_id = opt(get(2));
                let submit_time: f64 = get(3).parse().unwrap_or(0.0);
                let work_dir = get(4);
                let snapshot_path = get(5);
                let git_hash = get(6);
                let git_diff = get(7);
                let submit_script = opt(get(8));
                let submit_argv = opt(get(9));
                let env_vars = opt(get(10));
                let is_deleted: i64 = get(11).parse().unwrap_or(0);
                let updated_at: f64 = get(12).parse().unwrap_or(0.0);
                tx.execute(
                    "INSERT INTO jobs (
                        server_alias, job_id, array_base_id, array_task_id,
                        submit_time, work_dir, snapshot_path,
                        git_hash, git_diff,
                        submit_script, submit_argv,
                        env_vars,
                        is_deleted, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(server_alias, job_id) DO UPDATE SET
                        array_base_id=excluded.array_base_id,
                        array_task_id=excluded.array_task_id,
                        submit_time=excluded.submit_time,
                        work_dir=excluded.work_dir,
                        snapshot_path=excluded.snapshot_path,
                        git_hash=excluded.git_hash,
                        git_diff=excluded.git_diff,
                        submit_script=excluded.submit_script,
                        submit_argv=excluded.submit_argv,
                        env_vars=excluded.env_vars,
                        is_deleted=excluded.is_deleted,
                        updated_at=excluded.updated_at",
                    params![
                        alias_owned,
                        job_id,
                        array_base_id,
                        array_task_id,
                        submit_time,
                        work_dir,
                        snapshot_path,
                        git_hash,
                        git_diff,
                        submit_script,
                        submit_argv,
                        env_vars,
                        is_deleted,
                        updated_at,
                    ],
                )?;
                count += 1;
            }
            tx.commit()?;
            Ok(count)
        })
        .await?;
    Ok(inserted)
}

fn opt(s: &str) -> Option<&str> {
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}
