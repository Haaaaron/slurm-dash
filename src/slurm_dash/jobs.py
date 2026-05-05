"""Service layer for job actions that span local + remote state.

Soft delete is the user-facing "remove from view" action: it sets
``is_deleted=1`` on both the local replica and the remote DB and removes
the remote tarball. The remote row stays so other clients see the change
on their next sync. Hard removal of rows is handled by ``slurm-dash purge``.
"""

import shlex
import textwrap
import time

from .config import load_config, get_db_connection
from .remote_manager import run_ssh, RemoteManagerError


_DELETE_SCRIPT = textwrap.dedent("""
    import os, sqlite3, sys
    job_id = sys.argv[1]
    updated_at = float(sys.argv[2])
    db = os.path.expanduser('~/.slurm_tracker/.slurm_tracker.db')
    if not os.path.exists(db):
        db = os.path.expanduser('~/.slurm_tracker.db')
    if os.path.exists(db):
        c = sqlite3.connect(db, timeout=60)
        row = c.execute('SELECT snapshot_path FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
        c.execute('UPDATE jobs SET is_deleted = 1, updated_at = ? WHERE job_id = ?',
                  (updated_at, job_id))
        c.commit()
        c.close()
        if row and row[0]:
            try: os.remove(row[0])
            except OSError: pass
""").strip()


def delete_job(alias: str, job_id: str) -> tuple[bool, str]:
    """Soft-delete a job locally and on the remote (async).

    Local deletion happens immediately; remote deletion runs in background.
    Returns (ok, message) for local operation only.
    """
    import threading
    now = time.time()

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET is_deleted = 1, updated_at = ? "
            "WHERE server_alias = ? AND job_id = ?",
            (now, alias, job_id),
        )
        conn.execute(
            "DELETE FROM outputs WHERE server_alias = ? AND job_id = ?",
            (alias, job_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Remote deletion in background
    config = load_config()
    server = config.get("servers", {}).get(alias) or {}
    ssh_string = server.get("ssh_string")
    if ssh_string:
        def _remote_delete():
            cmd = (
                f"python3 -c {shlex.quote(_DELETE_SCRIPT)} "
                f"{shlex.quote(str(job_id))} {now}"
            )
            try:
                run_ssh(ssh_string, cmd, check=False, timeout=30)
            except Exception:
                pass
        threading.Thread(target=_remote_delete, daemon=True).start()

    return True, "deleted"


def delete_jobs(alias: str, job_ids: list) -> tuple[bool, str]:
    """Soft-delete multiple jobs locally and on the remote (async).

    Local deletion happens immediately; remote deletion runs in background.
    """
    import threading
    now = time.time()

    conn = get_db_connection()
    try:
        for job_id in job_ids:
            conn.execute(
                "UPDATE jobs SET is_deleted = 1, updated_at = ? "
                "WHERE server_alias = ? AND job_id = ?",
                (now, alias, job_id),
            )
            conn.execute(
                "DELETE FROM outputs WHERE server_alias = ? AND job_id = ?",
                (alias, job_id),
            )
        conn.commit()
    finally:
        conn.close()

    # Remote deletion in background
    config = load_config()
    server = config.get("servers", {}).get(alias) or {}
    ssh_string = server.get("ssh_string")
    if ssh_string:
        def _remote_delete_batch():
            for job_id in job_ids:
                cmd = (
                    f"python3 -c {shlex.quote(_DELETE_SCRIPT)} "
                    f"{shlex.quote(str(job_id))} {now}"
                )
                try:
                    run_ssh(ssh_string, cmd, check=False, timeout=30)
                except Exception:
                    pass
        threading.Thread(target=_remote_delete_batch, daemon=True).start()

    return True, f"deleted {len(job_ids)} jobs"


def add_tags(alias: str, job_ids: list[str], tag_name: str) -> None:
    """Add a tag to multiple jobs."""
    if not tag_name.strip():
        return
    tag_name = tag_name.strip()
    conn = get_db_connection()
    try:
        for job_id in job_ids:
            conn.execute(
                "INSERT OR IGNORE INTO tags (server_alias, job_id, tag_name) VALUES (?, ?, ?)",
                (alias, job_id, tag_name),
            )
        conn.commit()
    finally:
        conn.close()


def remove_tag(alias: str, job_id: str, tag_name: str) -> None:
    """Remove a tag from a single job."""
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM tags WHERE server_alias = ? AND job_id = ? AND tag_name = ?",
            (alias, job_id, tag_name),
        )
        conn.commit()
    finally:
        conn.close()


def list_tags_for_job(alias: str, job_id: str) -> list[str]:
    """Get all tags for a job, sorted alphabetically."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT tag_name FROM tags WHERE server_alias = ? AND job_id = ? ORDER BY tag_name",
            (alias, job_id),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def list_all_tags(alias: str) -> list[str]:
    """Get all unique tags for a server alias, sorted alphabetically."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT tag_name FROM tags WHERE server_alias = ? ORDER BY tag_name",
            (alias,),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def get_jobs_by_tag(alias: str, tag_name: str) -> list[str]:
    """Get all job IDs with a specific tag."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT job_id FROM tags WHERE server_alias = ? AND tag_name = ?",
            (alias, tag_name),
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]
