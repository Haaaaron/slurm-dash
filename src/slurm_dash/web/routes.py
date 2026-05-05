from __future__ import annotations

import json
import re
import shlex
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import get_db_connection, load_config
from ..jobs import delete_job as _delete_job
from ..output_probe import load_outputs, run_full_probe
from ..remote_manager import run_ssh
from ..slurm_api import get_live_status, _parse_gpus_from_gres
from ..sync_engine import sync_server

from .app import templates

router = APIRouter()

# ── constants ─────────────────────────────────────────────────────────────────

_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL",
    "BOOT_FAIL", "DEADLINE", "OUT_OF_MEMORY", "PREEMPTED", "REVOKED",
    "SPECIAL_EXIT",
})

_STATE_CLASS = {
    "RUNNING":    "bg-green-950  text-green-300",
    "PENDING":    "bg-yellow-950 text-yellow-300",
    "COMPLETED":  "bg-gray-800   text-gray-400",
    "FAILED":     "bg-red-950    text-red-300",
    "CANCELLED":  "bg-red-950    text-red-300",
    "COMPLETING": "bg-cyan-950   text-cyan-300",
    "TIMEOUT":    "bg-orange-950 text-orange-300",
    "PREEMPTED":  "bg-purple-950 text-purple-300",
}

_KIND_CLASS = {
    "slurm-out":          "bg-green-950  text-green-300",
    "slurm-err":          "bg-red-950    text-red-300",
    "inferred-redirect":  "bg-yellow-950 text-yellow-300",
    "from-slurm-log":     "bg-cyan-950   text-cyan-300",
    "from-workdir":       "bg-purple-950 text-purple-300",
}

# ── helpers ───────────────────────────────────────────────────────────────────

def _is_terminal(state: str) -> bool:
    return bool(state) and state.split()[0].upper() in _TERMINAL_STATES


def _fmt_dt(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _fmt_bytes(n) -> str:
    if n is None:
        return ""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def _parse_mem_gb(s: str) -> float:
    if not s or s in ("N/A", "(null)", ""):
        return 0.0
    s = s.strip()
    if s and s[-1].lower() in ("c", "n"):
        s = s[:-1]
    mul = {"k": 1 / 1e6, "m": 1 / 1e3, "g": 1.0, "t": 1024.0}
    if s and s[-1].lower() in mul:
        try:
            return float(s[:-1]) * mul[s[-1].lower()]
        except ValueError:
            return 0.0
    try:
        return float(s) / 1e9
    except ValueError:
        return 0.0


def _parse_slurm_time(s: str) -> int | None:
    s = (s or "").strip()
    if not s or s in ("UNLIMITED", "INVALID", "NOT_SET", "N/A", "Partition_Limit"):
        return None
    days = 0
    if "-" in s:
        d, _, rest = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return None
        s = rest
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, sec = nums
    elif len(nums) == 2:
        h, m, sec = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, sec = 0, 0, nums[0]
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + sec


def _fmt_duration(secs: int) -> str:
    if secs < 0:
        return "0m"
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _state_class(state: str) -> str:
    base = state.split()[0].upper() if state else ""
    return _STATE_CLASS.get(base, "bg-gray-800 text-gray-400")


def _load_jobs(alias: str) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT job_id, array_base_id, submit_time, work_dir, git_hash, "
            "snapshot_path, submit_argv, env_vars, "
            "final_state, final_cpus, final_req_mem, final_max_rss, "
            "final_gpus, final_node_list "
            "FROM jobs WHERE server_alias = ? AND is_deleted = 0 "
            "ORDER BY submit_time DESC",
            (alias,),
        ).fetchall()
    finally:
        conn.close()
    cols = [
        "job_id", "array_base_id", "submit_time", "work_dir", "git_hash",
        "snapshot_path", "submit_argv", "env_vars",
        "final_state", "final_cpus", "final_req_mem", "final_max_rss",
        "final_gpus", "final_node_list",
    ]
    return [dict(zip(cols, r)) for r in rows]


def _oldest_unfinished(alias: str) -> float | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT MIN(submit_time) FROM jobs "
            "WHERE server_alias = ? AND is_deleted = 0 "
            "AND (final_state IS NULL OR final_state = '')",
            (alias,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def _finalize_terminal(alias: str, status_map: dict) -> None:
    conn = get_db_connection()
    try:
        for job_id, st in status_map.items():
            if _is_terminal(st.get("state", "")):
                conn.execute(
                    "UPDATE jobs SET final_state=?, final_cpus=?, final_req_mem=?, "
                    "final_max_rss=?, final_gpus=?, final_node_list=? "
                    "WHERE server_alias=? AND job_id=? "
                    "AND (final_state IS NULL OR final_state='')",
                    (
                        st["state"], st.get("cpus"), st.get("req_mem"),
                        st.get("max_rss"), st.get("gpus", 0), st.get("node_list"),
                        alias, job_id,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _prepare_rows(jobs: list[dict], status_map: dict | None) -> list[dict]:
    rows = []
    for job in jobs:
        jid = job["job_id"]
        live = (status_map or {}).get(jid, {})

        if job.get("final_state"):
            state = job["final_state"]
            cpus      = job.get("final_cpus") or ""
            req_mem   = job.get("final_req_mem") or ""
            max_rss   = job.get("final_max_rss") or ""
            gpus      = job.get("final_gpus") or 0
            node_list = job.get("final_node_list") or ""
        else:
            state     = live.get("state") or ("" if status_map is None else "COMPLETED")
            cpus      = live.get("cpus") or ""
            req_mem   = live.get("req_mem") or ""
            max_rss   = live.get("max_rss") or ""
            gpus      = live.get("gpus") or 0
            node_list = live.get("node_list") or ""

        submit_argv = []
        if job.get("submit_argv"):
            try:
                submit_argv = json.loads(job["submit_argv"])
            except Exception:
                pass

        rows.append({
            "job_id":       jid,
            "array":        job.get("array_base_id") or "",
            "state":        state,
            "state_class":  _state_class(state),
            "submit_time":  _fmt_dt(job.get("submit_time")),
            "work_dir":     job.get("work_dir") or "",
            "git_hash":     (job.get("git_hash") or "")[:7],
            "cpus":         cpus,
            "req_mem":      req_mem,
            "max_rss":      max_rss,
            "gpus":         gpus,
            "node_list":    node_list,
            "snapshot_path": job.get("snapshot_path") or "",
            "submit_cmd":   "sbatch " + shlex.join(str(a) for a in submit_argv) if submit_argv else "",
        })
    return rows


def _compute_usage(rows: list[dict]) -> dict:
    running = pending = cpus = gpus = 0
    mem_gb = 0.0
    for r in rows:
        s = r["state"]
        if s == "RUNNING":
            running += 1
            try:
                cpus += int(r.get("cpus") or 0)
            except (TypeError, ValueError):
                pass
            gpus += int(r.get("gpus") or 0)
            mem_gb += _parse_mem_gb(r.get("req_mem") or "")
        elif s == "PENDING":
            pending += 1
    return {
        "running": running, "pending": pending, "total": len(rows),
        "cpus": cpus, "gpus": gpus, "mem_gb": round(mem_gb, 1),
    }


_ENV_VAR_RE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand_env(text: str, env: dict) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))
    return _ENV_VAR_RE.sub(repl, text)


def _build_file_tree(paths: list[str]) -> list[dict]:
    """Convert a flat tar path list into a nested [{name, path, is_dir, children}] tree."""
    nodes: dict[str, dict] = {}

    for raw in paths:
        is_dir = raw.endswith("/")
        clean = raw.rstrip("/")
        if clean.startswith("./"):
            clean = clean[2:]
        if not clean or clean == ".":
            continue
        parts = clean.split("/")

        for depth in range(len(parts)):
            key = "/".join(parts[: depth + 1])
            is_node_dir = depth < len(parts) - 1 or is_dir
            if key not in nodes:
                nodes[key] = {
                    "name": parts[depth],
                    "path": key,
                    "is_dir": is_node_dir,
                    "parent": "/".join(parts[:depth]) if depth else "",
                    "_child_keys": set(),
                }
            elif is_node_dir:
                nodes[key]["is_dir"] = True

        for depth in range(1, len(parts)):
            p = "/".join(parts[:depth])
            c = "/".join(parts[: depth + 1])
            if p in nodes:
                nodes[p]["_child_keys"].add(c)

    def make_node(key: str) -> dict:
        n = nodes[key]
        child_keys = n["_child_keys"]
        dirs  = sorted([k for k in child_keys if nodes[k]["is_dir"]],     key=lambda k: nodes[k]["name"].lower())
        files = sorted([k for k in child_keys if not nodes[k]["is_dir"]], key=lambda k: nodes[k]["name"].lower())
        return {
            "name":             n["name"],
            "path":             n["path"] if not n["is_dir"] else "",
            "is_dir":           n["is_dir"],
            "is_submit_script": n["path"] == "submit_script.sh" and not n["is_dir"],
            "children":         [make_node(k) for k in dirs + files],
        }

    root_keys = [k for k, v in nodes.items() if not v["parent"]]
    dirs  = sorted([k for k in root_keys if nodes[k]["is_dir"]],     key=lambda k: nodes[k]["name"].lower())
    files = sorted([k for k in root_keys if not nodes[k]["is_dir"]], key=lambda k: nodes[k]["name"].lower())
    return [make_node(k) for k in dirs + files]


# ── page routes ───────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    config = load_config()
    aliases = list(config.get("servers", {}).keys())
    servers = {}
    for alias in aliases:
        jobs = _load_jobs(alias)
        rows = _prepare_rows(jobs, None)
        servers[alias] = {"rows": rows, "usage": _compute_usage(rows)}
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"aliases": aliases, "servers": servers},
    )


# ── job table partial ─────────────────────────────────────────────────────────

@router.get("/jobs/{alias}", response_class=HTMLResponse)
def jobs_table(request: Request, alias: str):
    jobs = _load_jobs(alias)
    since = _oldest_unfinished(alias)
    status_map = get_live_status(alias, since)
    if status_map:
        _finalize_terminal(alias, status_map)
    rows = _prepare_rows(jobs, status_map)
    return templates.TemplateResponse(
        request, "partials/jobs_table.html",
        {"alias": alias, "rows": rows, "usage": _compute_usage(rows)},
    )


@router.post("/sync/{alias}", response_class=HTMLResponse)
def sync_alias(request: Request, alias: str):
    config = load_config()
    server = config.get("servers", {}).get(alias, {})
    ssh_string = server.get("ssh_string")
    if ssh_string:
        sync_server(alias, ssh_string)
        since = _oldest_unfinished(alias)
        status_map = get_live_status(alias, since)
        _finalize_terminal(alias, status_map)
    else:
        status_map = {}
    jobs = _load_jobs(alias)
    rows = _prepare_rows(jobs, status_map)
    return templates.TemplateResponse(
        request, "partials/jobs_table.html",
        {"alias": alias, "rows": rows, "usage": _compute_usage(rows)},
    )


# ── job actions ───────────────────────────────────────────────────────────────

@router.delete("/jobs/{alias}/{job_id}", response_class=HTMLResponse)
def delete_job(alias: str, job_id: str):
    _delete_job(alias, job_id)
    return HTMLResponse("")


# ── files modal ───────────────────────────────────────────────────────────────

@router.get("/jobs/{alias}/{job_id}/files", response_class=HTMLResponse)
def files_modal(request: Request, alias: str, job_id: str):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT submit_argv, work_dir, snapshot_path, env_vars "
            "FROM jobs WHERE server_alias=? AND job_id=?",
            (alias, job_id),
        ).fetchone()
    finally:
        conn.close()

    submit_cmd = work_dir = snapshot_path = ""
    env_vars: dict = {}
    if row:
        if row[0]:
            try:
                argv = json.loads(row[0])
                submit_cmd = "sbatch " + shlex.join(str(a) for a in argv)
            except Exception:
                pass
        work_dir      = row[1] or ""
        snapshot_path = row[2] or ""
        if row[3]:
            try:
                env_vars = json.loads(row[3])
            except Exception:
                pass

    return templates.TemplateResponse(
        request, "partials/files_modal.html",
        {
            "alias": alias, "job_id": job_id,
            "submit_cmd": submit_cmd, "work_dir": work_dir,
            "has_snapshot": bool(snapshot_path),
        },
    )


@router.get("/jobs/{alias}/{job_id}/snapshot", response_class=HTMLResponse)
def snapshot_file_list(request: Request, alias: str, job_id: str):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT snapshot_path FROM jobs WHERE server_alias=? AND job_id=?",
            (alias, job_id),
        ).fetchone()
    finally:
        conn.close()

    config = load_config()
    ssh = (config.get("servers", {}).get(alias) or {}).get("ssh_string", "")
    files: list[str] = []
    error = ""

    tree: list[dict] = []
    error = ""

    if row and row[0] and ssh:
        try:
            result = run_ssh(ssh, f"tar -tzf {shlex.quote(row[0])}", check=False, timeout=15)
            if result.returncode == 0:
                tree = _build_file_tree([f for f in result.stdout.splitlines() if f])
            else:
                error = result.stderr.strip() or "tar failed"
        except Exception as e:
            error = str(e)
    elif not row or not row[0]:
        error = "No snapshot available for this job."
    elif not ssh:
        error = "No SSH connection configured."

    return templates.TemplateResponse(
        request, "partials/snapshot_file_list.html",
        {"alias": alias, "job_id": job_id, "tree": tree, "error": error},
    )


@router.get("/jobs/{alias}/{job_id}/snapshot/file", response_class=HTMLResponse)
def snapshot_file_content(request: Request, alias: str, job_id: str, path: str = "", expand_env: int = 0):
    if not path:
        return HTMLResponse("<p class='text-gray-500 italic p-4'>Select a file to preview.</p>")

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT snapshot_path, env_vars FROM jobs WHERE server_alias=? AND job_id=?",
            (alias, job_id),
        ).fetchone()
    finally:
        conn.close()

    config = load_config()
    ssh = (config.get("servers", {}).get(alias) or {}).get("ssh_string", "")

    content = error = ""
    if row and row[0] and ssh:
        try:
            cmd = f"tar -xzOf {shlex.quote(row[0])} {shlex.quote(path)} | head -c 262144"
            result = run_ssh(ssh, cmd, check=False, timeout=15)
            if result.returncode == 0:
                raw = result.stdout
                if "\x00" in raw[:4096]:
                    content = "(binary file — preview not available)"
                else:
                    content = raw
                    if expand_env and row[1]:
                        try:
                            env = json.loads(row[1])
                            content = _expand_env(content, env)
                        except Exception:
                            pass
            else:
                error = result.stderr.strip() or "Failed to extract file."
        except Exception as e:
            error = str(e)
    else:
        error = "Snapshot not available."

    return templates.TemplateResponse(
        request, "partials/snapshot_preview.html",
        {"path": path, "content": content, "error": error},
    )


# ── outputs tab ───────────────────────────────────────────────────────────────

@router.get("/jobs/{alias}/{job_id}/outputs", response_class=HTMLResponse)
def outputs_tab(request: Request, alias: str, job_id: str):
    rows = load_outputs(alias, job_id)
    return _outputs_response(request, alias, job_id, rows)


@router.post("/jobs/{alias}/{job_id}/reprobe", response_class=HTMLResponse)
def reprobe(request: Request, alias: str, job_id: str):
    run_full_probe(alias, job_id)
    rows = load_outputs(alias, job_id)
    return _outputs_response(request, alias, job_id, rows)


def _outputs_response(request: Request, alias: str, job_id: str, rows: list[dict]):
    enhanced = []
    for r in rows:
        enhanced.append({
            **r,
            "kind_class":  _KIND_CLASS.get(r["kind"], "bg-gray-800 text-gray-400"),
            "size_fmt":    _fmt_bytes(r.get("size_bytes")),
            "mtime_fmt":   _fmt_dt(r.get("mtime")),
            "exists_text": ("yes" if r.get("exists_remote") else "missing") if r.get("probed_at") else "?",
            "exists_class": (
                "text-green-400" if r.get("exists_remote") and r.get("probed_at")
                else "text-red-400" if r.get("probed_at")
                else "text-gray-500"
            ),
            "local_text":  "synced" if r.get("local_path") else ("remote" if r.get("exists_remote") and not r.get("is_dir") else ""),
            "local_class": "text-green-400" if r.get("local_path") else "text-gray-500",
        })

    if not rows:
        status = "No outputs recorded — press Sync to run the inferrer + SSH probe."
    elif any(r.get("probed_at") for r in rows):
        synced = sum(1 for r in rows if r.get("local_path"))
        last_probe = max((r.get("probed_at") or 0) for r in rows)
        status = f"{len(rows)} entries · {synced} synced locally · last probe {_fmt_dt(last_probe)}"
    else:
        status = f"{len(rows)} inferred entries (not yet probed) — press Sync."

    return templates.TemplateResponse(
        request, "partials/outputs_tab.html",
        {"alias": alias, "job_id": job_id, "rows": enhanced, "status": status},
    )


# ── squeue modal ──────────────────────────────────────────────────────────────

@router.get("/squeue/{alias}", response_class=HTMLResponse)
def squeue_modal(request: Request, alias: str):
    config = load_config()
    ssh = (config.get("servers", {}).get(alias) or {}).get("ssh_string", "")
    jobs: list[dict] = []
    error = ""

    if not ssh:
        error = "No SSH connection configured."
    else:
        cmd = "squeue --me --noheader --format='%i|%T|%P|%j|%M|%l|%R|%C|%b'"
        try:
            result = run_ssh(ssh, cmd, check=False, timeout=12)
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    fields = line.strip().split("|")
                    if len(fields) < 9:
                        continue
                    jid, state, partition, name, t_used, t_limit, node_reason, cpus, gres = fields[:9]
                    used_s  = _parse_slurm_time(t_used)
                    limit_s = _parse_slurm_time(t_limit)
                    gpus    = _parse_gpus_from_gres(gres)
                    progress = None
                    if state == "RUNNING" and used_s is not None and limit_s:
                        pct = min(100, int(100 * used_s / limit_s))
                        remaining = max(0, limit_s - used_s)
                        progress = {
                            "pct": pct,
                            "remaining": _fmt_duration(remaining),
                            "color": "bg-red-500" if pct >= 90 else "bg-yellow-400" if pct >= 75 else "bg-green-500",
                        }
                    jobs.append({
                        "job_id": jid, "state": state, "state_class": _state_class(state),
                        "partition": partition, "name": name,
                        "time_used": t_used, "time_limit": t_limit,
                        "node_reason": node_reason, "cpus": cpus, "gpus": gpus,
                        "progress": progress,
                    })
            else:
                error = result.stderr.strip() or "squeue failed"
        except Exception as e:
            error = str(e)

    jobs.sort(key=lambda j: (j["state"] != "RUNNING", j["state"], j["job_id"]))
    running = sum(1 for j in jobs if j["state"] == "RUNNING")
    pending = sum(1 for j in jobs if j["state"] == "PENDING")

    return templates.TemplateResponse(
        request, "partials/squeue_modal.html",
        {
            "alias": alias, "jobs": jobs, "error": error,
            "running": running, "pending": pending, "total": len(jobs),
        },
    )
