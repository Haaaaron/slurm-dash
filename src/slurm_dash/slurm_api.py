import re
import subprocess
import shlex
import time
from datetime import datetime
from .config import load_config, get_db_connection
from .remote_manager import run_ssh

_GPU_TRES_RE = re.compile(r"gres/gpu(?::([^=,]+))?=(\d+)")
_GPU_GRES_RE = re.compile(r"gpu(?::([^:=,(]+))?[:=](\d+)")
_WORKDIR_RE = re.compile(r"^\s*WorkDir=(.+)$", re.MULTILINE)


def _parse_gpus_from_tres(tres: str) -> tuple[int, str]:
    if not tres:
        return 0, ""
    models = set()
    counts = []
    for m in re.finditer(_GPU_TRES_RE, tres):
        model = m.group(1) or ""
        count = int(m.group(2))
        if model:
            models.add(model.upper())
        counts.append(count)
    return sum(counts), "/".join(sorted(models))


def _parse_gpus_from_gres(gres: str) -> tuple[int, str]:
    if not gres or gres in ("(null)", "N/A"):
        return 0, ""
    models = set()
    counts = []
    for m in re.finditer(_GPU_GRES_RE, gres):
        model = m.group(1) or ""
        count = int(m.group(2))
        if model:
            models.add(model.upper())
        counts.append(count)
    return sum(counts), "/".join(sorted(models))

def update_workdir_from_scontrol(alias: str, job_id: str, ssh_string: str) -> bool:
    """Query scontrol for a job's WorkDir and update the database if found.

    Returns True if successful, False otherwise.
    """
    try:
        result = run_ssh(ssh_string, f"scontrol show job {job_id}", check=False, timeout=5)
        if result.returncode != 0:
            return False

        m = _WORKDIR_RE.search(result.stdout)
        if not m:
            return False

        work_dir = m.group(1).strip()
        if not work_dir:
            return False

        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE jobs SET work_dir = ?, updated_at = ? WHERE server_alias = ? AND job_id = ?",
                (work_dir, time.time(), alias, job_id),
            )
            conn.commit()
        finally:
            conn.close()

        return True
    except Exception:
        return False


def get_live_status(alias: str, since: float | None = None) -> dict:
    """
    Fetches live and historical status of jobs for the given server alias.
    Returns a dict mapping JobID -> { "state": ..., "cpus": ..., "req_mem": ... }

    `since` is a unix timestamp; only queries recent jobs to avoid heavy sacct queries.
    Falls back to today when None (no 90-day lookback to avoid HPC admin issues).
    """
    config = load_config()
    server_info = config.get("servers", {}).get(alias)
    if not server_info:
        return {}

    ssh_string = server_info.get("ssh_string")
    if not ssh_string:
        return {}

    if since is None:
        since = time.time() - 86400  # Only today, not 90 days
    # Buffer back one day to avoid edge cases at the boundary.
    since_str = datetime.fromtimestamp(max(0, since - 86400)).strftime("%Y-%m-%d")

    # Command to get squeue (running/pending) and minimal sacct (today only).
    # Only query essential fields to minimize load on the HPC system.
    cmd = (
        "squeue -u $USER --noheader --format=\"%i|%T|%b|%R\" 2>/dev/null || true; "
        "echo \"---\"; "
        f"sacct -X --parsable2 -S {since_str} "
        "--format=\"JobID,State,AllocCPUs,ReqMem,AllocTRES,NodeList\" 2>/dev/null || true"
    )

    try:
        result = run_ssh(ssh_string, cmd, check=False, timeout=15)
        if result.returncode != 0:
            return {}
            
        stdout = result.stdout.strip()
        parts = stdout.split("---")
        
        squeue_out = parts[0].strip() if len(parts) > 0 else ""
        sacct_out = parts[1].strip() if len(parts) > 1 else ""
        
        status_map = {}
        
        # Parse sacct first (historical & running)
        for line in sacct_out.splitlines():
            if not line.strip() or line.startswith('JobID|'):
                continue
            cols = line.strip().split('|')
            if len(cols) >= 4:
                job_id = cols[0].split('_')[0] # handle arrays if sacct formats them as ID_TASK
                state = cols[1]
                cpus = cols[2]
                req_mem = cols[3]
                tres = cols[4] if len(cols) >= 5 else ""
                node_list = cols[5] if len(cols) >= 6 else ""
                gpu_count, gpu_model = _parse_gpus_from_tres(tres)
                status_map[job_id] = {
                    "state": state,
                    "cpus": cpus,
                    "req_mem": req_mem,
                    "gpus": gpu_count,
                    "gpu_model": gpu_model,
                    "node_list": node_list,
                }

        # Parse squeue (live overlays historical)
        for line in squeue_out.splitlines():
            if not line.strip() or line.startswith('JobID|'):
                continue
            cols = line.strip().split('|')
            if len(cols) >= 2:
                job_id = cols[0].split('_')[0]
                state = cols[1]
                gres = cols[2] if len(cols) >= 3 else ""
                node_or_reason = cols[3] if len(cols) >= 4 else ""
                gpu_count, gpu_model = _parse_gpus_from_gres(gres)
                if job_id not in status_map:
                    status_map[job_id] = {
                        "state": state, "cpus": "N/A",
                        "req_mem": "N/A",
                        "gpus": gpu_count,
                        "gpu_model": gpu_model,
                        "node_list": node_or_reason,
                    }
                else:
                    status_map[job_id]["state"] = state # squeue is more realtime
                    if not status_map[job_id].get("gpus"):
                        status_map[job_id]["gpus"] = gpu_count
                        status_map[job_id]["gpu_model"] = gpu_model
                    # squeue is more current for nodes too, esp. for RUNNING jobs
                    if node_or_reason and node_or_reason not in ("(null)", "N/A"):
                        status_map[job_id]["node_list"] = node_or_reason

        return status_map
    except Exception:
        return {}
