import re
import subprocess
import shlex
import time
from datetime import datetime
from .config import load_config
from .remote_manager import run_ssh

_GPU_TRES_RE = re.compile(r"gres/gpu(?::[^=,]+)?=(\d+)")
_GPU_GRES_RE = re.compile(r"gpu(?::[^:=,(]+)?[:=](\d+)")


def _parse_gpus_from_tres(tres: str) -> int:
    if not tres:
        return 0
    return sum(int(n) for n in _GPU_TRES_RE.findall(tres))


def _parse_gpus_from_gres(gres: str) -> int:
    if not gres or gres in ("(null)", "N/A"):
        return 0
    n = sum(int(x) for x in _GPU_GRES_RE.findall(gres))
    return n

def get_live_status(alias: str, since: float | None = None) -> dict:
    """
    Fetches live and historical status of jobs for the given server alias.
    Returns a dict mapping JobID -> { "state": ..., "cpus": ..., "req_mem": ..., "max_rss": ... }

    `since` is a unix timestamp; passed to sacct -S so historical jobs older
    than today still show up (sacct defaults to start-of-day). Falls back
    to 90 days when None.
    """
    config = load_config()
    server_info = config.get("servers", {}).get(alias)
    if not server_info:
        return {}

    ssh_string = server_info.get("ssh_string")
    if not ssh_string:
        return {}

    if since is None:
        since = time.time() - 90 * 86400
    # Buffer back one day to avoid edge cases at the boundary.
    since_str = datetime.fromtimestamp(max(0, since - 86400)).strftime("%Y-%m-%d")

    # Command to get squeue and sacct info. AllocTRES carries gres/gpu=N for
    # running/historical jobs; squeue %b carries the GRES request for pending.
    # %R is NodeList for running, Reason for pending.
    cmd = (
        "squeue -u $USER --noheader --format=\"%i|%T|%b|%R\" && echo \"---\" && "
        f"sacct -X --parsable2 -S {since_str} "
        "--format=\"JobID,State,AllocCPUs,ReqMem,MaxRSS,AllocTRES%128,NodeList%64\""
    )
    
    try:
        result = run_ssh(ssh_string, cmd, check=False, timeout=5)
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
            if len(cols) >= 5:
                job_id = cols[0].split('_')[0] # handle arrays if sacct formats them as ID_TASK
                state = cols[1]
                cpus = cols[2]
                req_mem = cols[3]
                max_rss = cols[4]
                tres = cols[5] if len(cols) >= 6 else ""
                node_list = cols[6] if len(cols) >= 7 else ""
                status_map[job_id] = {
                    "state": state,
                    "cpus": cpus,
                    "req_mem": req_mem,
                    "max_rss": max_rss,
                    "gpus": _parse_gpus_from_tres(tres),
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
                if job_id not in status_map:
                    status_map[job_id] = {
                        "state": state, "cpus": "N/A",
                        "req_mem": "N/A", "max_rss": "N/A",
                        "gpus": _parse_gpus_from_gres(gres),
                        "node_list": node_or_reason,
                    }
                else:
                    status_map[job_id]["state"] = state # squeue is more realtime
                    if not status_map[job_id].get("gpus"):
                        status_map[job_id]["gpus"] = _parse_gpus_from_gres(gres)
                    # squeue is more current for nodes too, esp. for RUNNING jobs
                    if node_or_reason and node_or_reason not in ("(null)", "N/A"):
                        status_map[job_id]["node_list"] = node_or_reason

        return status_map
    except Exception:
        return {}
