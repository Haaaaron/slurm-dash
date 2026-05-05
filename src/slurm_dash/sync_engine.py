import csv
import io
import shlex
import textwrap
import time

from .config import load_config, get_db_connection
from .remote_manager import run_ssh
from .slurm_api import update_workdir_from_scontrol

REMOTE_COLUMNS = [
    "job_id", "array_base_id", "array_task_id",
    "submit_time", "work_dir", "snapshot_path",
    "git_hash", "git_diff",
    "submit_script", "submit_argv",
    "env_vars",
    "is_deleted", "updated_at",
]
# Columns that may be missing on older remote schemas; fallback fills them
# with empty strings so the local insert still works.
_OPTIONAL_REMOTE_COLS = ("submit_script", "submit_argv", "env_vars")

_REMOTE_SCRIPT = textwrap.dedent(f"""
    import os, sqlite3, csv, sys
    cutoff = float(sys.argv[1])
    cols = {REMOTE_COLUMNS!r}
    optional = {_OPTIONAL_REMOTE_COLS!r}
    db = os.path.expanduser('~/.slurm_tracker/.slurm_tracker.db')
    if not os.path.exists(db):
        db = os.path.expanduser('~/.slurm_tracker.db')
    if os.path.exists(db):
        conn = sqlite3.connect(db, timeout=60)
        cur = conn.cursor()
        try:
            cur.execute('SELECT ' + ','.join(cols) + ' FROM jobs WHERE updated_at > ?', (cutoff,))
        except sqlite3.OperationalError:
            # Older schema missing one or more optional columns: fall back.
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
""").strip()


def sync_server(alias, ssh_string):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT MAX(updated_at) FROM jobs WHERE server_alias = ?", (alias,)
    )
    row = cursor.fetchone()
    max_updated = row[0] if row[0] is not None else 0

    cmd = f"python3 -c {shlex.quote(_REMOTE_SCRIPT)} {float(max_updated)}"

    try:
        result = run_ssh(ssh_string, cmd, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return

        reader = csv.reader(io.StringIO(result.stdout))
        for row in reader:
            if len(row) < len(REMOTE_COLUMNS):
                continue
            (job_id, array_base_id, array_task_id,
             submit_time, work_dir, snapshot_path,
             git_hash, git_diff,
             submit_script, submit_argv,
             env_vars,
             is_deleted, updated_at) = row[:len(REMOTE_COLUMNS)]

            cursor.execute("""
                INSERT INTO jobs (
                    server_alias, job_id, array_base_id, array_task_id,
                    submit_time, work_dir, snapshot_path,
                    git_hash, git_diff,
                    submit_script, submit_argv,
                    env_vars,
                    is_deleted, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at=excluded.updated_at
            """, (
                alias, job_id, array_base_id or None, array_task_id or None,
                float(submit_time or 0), work_dir, snapshot_path,
                git_hash, git_diff,
                submit_script or None, submit_argv or None,
                env_vars or None,
                int(is_deleted or 0), float(updated_at or 0),
            ))

        conn.commit()
    except Exception as e:
        print(f"Sync error for {alias}: {e}")
    finally:
        conn.close()

    # Query scontrol for WorkDir only on running/pending jobs (not historical).
    # This avoids expensive queries on completed jobs.
    try:
        conn = get_db_connection()
        # Only query jobs without final_state (still running/pending)
        active_jobs = conn.execute(
            "SELECT job_id FROM jobs WHERE server_alias = ? AND final_state IS NULL LIMIT 10",
            (alias,)
        ).fetchall()
        conn.close()

        if active_jobs and ssh_string:
            for (job_id,) in active_jobs:
                update_workdir_from_scontrol(alias, job_id, ssh_string)
    except Exception:
        pass


def sync_all():
    config = load_config()
    servers = config.get("servers", {})
    for alias, server_info in servers.items():
        if server_info.get("sync_on_startup", False):
            ssh_string = server_info.get("ssh_string")
            if ssh_string:
                sync_server(alias, ssh_string)
