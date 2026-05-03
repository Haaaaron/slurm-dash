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
    """Soft-delete a job locally and on the remote.

    Returns (ok, message). Local update happens unconditionally; remote
    failure is reported but doesn't reverse the local change (the next
    sync will reconcile).
    """
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

    config = load_config()
    server = config.get("servers", {}).get(alias) or {}
    ssh_string = server.get("ssh_string")
    if not ssh_string:
        return False, f"No ssh_string configured for {alias}; local-only delete."

    cmd = (
        f"python3 -c {shlex.quote(_DELETE_SCRIPT)} "
        f"{shlex.quote(str(job_id))} {now}"
    )
    try:
        result = run_ssh(ssh_string, cmd, check=False)
        if result.returncode != 0:
            return False, f"Remote delete failed: {result.stderr.strip()}"
    except RemoteManagerError as e:
        return False, str(e)

    return True, "deleted"
