"""Probe a list of remote paths via a single SSH call, plus enumerate
the workdir for files newer than submit_time and parse paths out of the
slurm-output. Files smaller than `max_download_mb` are scp'd into the
local cache; for larger files we keep only the remote path so the user
can `ssh <host>` and cd there.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import textwrap
import time

from .config import get_db_connection, load_config, CACHE_DIR
from .remote_manager import run_ssh

# Hard caps to keep the probe + UI responsive.
MAX_PROBE_PATHS = 64
MAX_HEAD_BYTES = 8192
MAX_LISTING = 50
MAX_WORKDIR_FILES = 100
MAX_LOG_PATHS = 64
DEFAULT_MAX_DOWNLOAD_MB = 50

_REMOTE_SCRIPT = textwrap.dedent("""
    import json, os, re, sys
    spec = json.loads(sys.argv[1])
    paths = spec.get("paths") or []
    workdir = spec.get("workdir") or ""
    submit_time = float(spec.get("submit_time") or 0)
    slurm_out = spec.get("slurm_out") or ""
    max_workdir = int(spec.get("max_workdir", 100))
    max_log_paths = int(spec.get("max_log_paths", 64))
    MAX_HEAD = int(spec.get("max_head", 8192))
    MAX_LIST = int(spec.get("max_listing", 50))

    def stat_record(p):
        rec = {"path": p, "exists": False, "is_dir": False,
               "size": None, "mtime": None, "head": None,
               "listing": None}
        try:
            st = os.lstat(p)
        except OSError:
            return rec
        rec["exists"] = True
        rec["size"] = st.st_size
        rec["mtime"] = st.st_mtime
        if os.path.isdir(p):
            rec["is_dir"] = True
            try:
                names = sorted(os.listdir(p))[:MAX_LIST]
                listing = []
                for n in names:
                    full = os.path.join(p, n)
                    try:
                        est = os.lstat(full)
                        listing.append({"name": n, "size": est.st_size,
                                        "mtime": est.st_mtime,
                                        "is_dir": os.path.isdir(full)})
                    except OSError:
                        listing.append({"name": n, "size": None,
                                        "mtime": None, "is_dir": False})
                rec["listing"] = listing
            except OSError:
                pass
        else:
            try:
                with open(p, "rb") as f:
                    rec["head"] = f.read(MAX_HEAD).decode(
                        "utf-8", errors="replace")
            except OSError:
                pass
        return rec

    out = {"probed": [], "from_workdir": [], "from_slurm_log": []}
    for p in paths:
        out["probed"].append(stat_record(p))

    # Files in workdir with mtime > submit_time. Skip noise dirs.
    SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv",
                 ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 ".slurm_tracker"}
    if workdir and os.path.isdir(workdir):
        found = []
        try:
            for root, dirs, files in os.walk(workdir, followlinks=False):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in SKIP_DIRS]
                for fname in files:
                    full = os.path.join(root, fname)
                    try:
                        st = os.lstat(full)
                    except OSError:
                        continue
                    if st.st_mtime <= submit_time:
                        continue
                    found.append({"path": full, "size": st.st_size,
                                  "mtime": st.st_mtime, "exists": True,
                                  "is_dir": False})
                    if len(found) >= max_workdir:
                        break
                if len(found) >= max_workdir:
                    break
        except OSError:
            pass
        out["from_workdir"] = found

    # Path tokens inside the slurm-out head text.
    log_rec = next((r for r in out["probed"] if r["path"] == slurm_out), None)
    if log_rec and log_rec.get("head"):
        pat = re.compile(
            r"(/(?:scratch|flash|projappl|tmp|work|users|home|var)/"
            r"[A-Za-z0-9_./%+-]+)"
        )
        seen = set()
        found = []
        for m in pat.finditer(log_rec["head"]):
            cand = m.group(1).rstrip(".,;:)")
            if cand in seen or cand == slurm_out:
                continue
            seen.add(cand)
            try:
                st = os.lstat(cand)
                if os.path.isdir(cand):
                    continue
                found.append({"path": cand, "exists": True,
                              "size": st.st_size, "mtime": st.st_mtime,
                              "is_dir": False})
            except OSError:
                continue
            if len(found) >= max_log_paths:
                break
        out["from_slurm_log"] = found

    sys.stdout.write(json.dumps(out))
""").strip()


def probe_and_collect(
    ssh_string: str,
    *,
    inferred_paths: list[str],
    workdir: str,
    submit_time: float,
    slurm_out_path: str,
) -> dict:
    """Single SSH call: probe inferred paths + workdir scan + log parse.

    Returns {"probed": [...], "from_workdir": [...], "from_slurm_log": [...]}.
    """
    paths = list(dict.fromkeys(inferred_paths))[:MAX_PROBE_PATHS]
    spec = {
        "paths": paths,
        "workdir": workdir or "",
        "submit_time": float(submit_time or 0),
        "slurm_out": slurm_out_path or "",
        "max_workdir": MAX_WORKDIR_FILES,
        "max_log_paths": MAX_LOG_PATHS,
        "max_head": MAX_HEAD_BYTES,
        "max_listing": MAX_LISTING,
    }
    cmd = (
        f"python3 -c {shlex.quote(_REMOTE_SCRIPT)} "
        f"{shlex.quote(json.dumps(spec))}"
    )
    try:
        result = run_ssh(ssh_string, cmd, check=False, timeout=45)
        if result.returncode != 0:
            return {"probed": [{"path": p, "exists": False} for p in paths],
                    "from_workdir": [], "from_slurm_log": []}
    except Exception:
        return {"probed": [{"path": p, "exists": False} for p in paths],
                "from_workdir": [], "from_slurm_log": []}
    try:
        return json.loads(result.stdout.strip() or "{}")
    except ValueError:
        return {"probed": [], "from_workdir": [], "from_slurm_log": []}


def _max_download_bytes() -> int:
    cfg = load_config().get("general", {})
    mb = int(cfg.get("max_download_mb", DEFAULT_MAX_DOWNLOAD_MB))
    return mb * 1024 * 1024


def _local_dir(server_alias: str, job_id: str):
    d = CACHE_DIR / "outputs" / server_alias / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_local_name(remote_path: str, taken: set) -> str:
    base = os.path.basename(remote_path) or "output"
    name = base
    i = 1
    while name in taken:
        name = f"{base}.{i}"
        i += 1
    taken.add(name)
    return name


def download_small_files(
    ssh_string: str,
    server_alias: str,
    job_id: str,
    candidates: list[dict],
    max_bytes: int,
) -> dict[str, str]:
    """scp each candidate (must have 'path' and 'size') to the local cache
    if size <= max_bytes. Returns {remote_path: local_path}.
    Larger files / dirs / missing files are skipped."""
    if not ssh_string:
        return {}
    eligible = []
    for c in candidates:
        if not c.get("exists") or c.get("is_dir"):
            continue
        size = c.get("size")
        if size is None or size > max_bytes:
            continue
        eligible.append(c)
    if not eligible:
        return {}
    out_dir = _local_dir(server_alias, job_id)
    taken: set = set()
    locals_: dict[str, str] = {}
    for c in eligible:
        remote = c["path"]
        name = _safe_local_name(remote, taken)
        local = out_dir / name
        try:
            subprocess.run(
                ["scp", "-q", f"{ssh_string}:{remote}", str(local)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=120, check=True,
            )
            locals_[remote] = str(local)
        except (subprocess.SubprocessError, OSError):
            try:
                if local.exists():
                    local.unlink()
            except OSError:
                pass
    return locals_


def store_outputs(
    server_alias: str,
    job_id: str,
    inferred: list[dict],
    probe_result: dict,
    local_paths: dict[str, str] | None = None,
) -> None:
    """Persist all known outputs for (alias, job_id), replacing prior rows.

    `inferred` carries the kind for paths derived from the submit script
    (slurm-out / slurm-err / inferred-redirect). `probe_result` carries
    runtime-discovered entries (from_workdir, from_slurm_log) and is also
    where we read the probe metadata for the inferred paths.
    """
    now = time.time()
    local_paths = dict(local_paths or {})
    probed_by_path = {p["path"]: p for p in probe_result.get("probed", [])}

    rows: list[tuple] = []
    seen_paths: set[str] = set()

    # Inferred-from-script paths first — these carry the most specific kind.
    for entry in inferred:
        path = entry["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        kind = entry["kind"]
        meta = probed_by_path.get(path) or {}
        rows.append((
            server_alias, job_id, kind, path,
            meta.get("size"), meta.get("mtime"),
            int(bool(meta.get("exists"))),
            int(bool(meta.get("is_dir"))),
            meta.get("head"),
            local_paths.get(path),
            now,
            now if meta else None,
        ))

    # Workdir-discovered files (skip duplicates of inferred).
    for r in probe_result.get("from_workdir", []):
        path = r["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        rows.append((
            server_alias, job_id, "from-workdir", path,
            r.get("size"), r.get("mtime"),
            1, int(bool(r.get("is_dir"))), None,
            local_paths.get(path),
            now, now,
        ))

    # Paths extracted from slurm-out body (skip duplicates).
    for r in probe_result.get("from_slurm_log", []):
        path = r["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        rows.append((
            server_alias, job_id, "from-slurm-log", path,
            r.get("size"), r.get("mtime"),
            int(bool(r.get("exists"))), int(bool(r.get("is_dir"))),
            None, local_paths.get(path),
            now, now,
        ))

    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM outputs WHERE server_alias = ? AND job_id = ?",
            (server_alias, job_id),
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO outputs (
                server_alias, job_id, kind, path,
                size_bytes, mtime, exists_remote, is_dir, head_text,
                local_path, discovered_at, probed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute(
            "UPDATE jobs SET output_probed_at = ? "
            "WHERE server_alias = ? AND job_id = ?",
            (now, server_alias, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def load_outputs(server_alias: str, job_id: str) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT kind, path, size_bytes, mtime, exists_remote, is_dir,
                   head_text, local_path, discovered_at, probed_at
            FROM outputs
            WHERE server_alias = ? AND job_id = ?
            ORDER BY
              CASE kind
                WHEN 'slurm-out' THEN 0
                WHEN 'slurm-err' THEN 1
                WHEN 'inferred-redirect' THEN 2
                WHEN 'from-slurm-log' THEN 3
                WHEN 'from-workdir' THEN 4
                ELSE 5
              END,
              path
            """,
            (server_alias, job_id),
        ).fetchall()
    finally:
        conn.close()
    cols = ["kind", "path", "size_bytes", "mtime", "exists_remote",
            "is_dir", "head_text", "local_path",
            "discovered_at", "probed_at"]
    return [dict(zip(cols, r)) for r in rows]


def run_full_probe(server_alias: str, job_id: str) -> bool:
    """Top-level: read job state from DB, infer + SSH probe + sync small
    files + store. Used by both the auto-finalize hook and the Re-probe
    button. Returns True on success.
    """
    from .output_inferrer import infer_outputs

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT submit_argv, submit_script, env_vars, work_dir, "
            "submit_time, final_node_list "
            "FROM jobs WHERE server_alias = ? AND job_id = ?",
            (server_alias, job_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    submit_argv, submit_script, env_vars, work_dir, submit_time, node_list = row

    inferred = infer_outputs(
        job_id=job_id,
        work_dir=work_dir or "",
        submit_argv_json=submit_argv,
        submit_script=submit_script,
        env_vars_json=env_vars,
        final_node_list=node_list,
    )
    config = load_config()
    ssh = (config.get("servers", {}).get(server_alias) or {}).get("ssh_string")

    slurm_out_path = next(
        (e["path"] for e in inferred if e["kind"] == "slurm-out"), ""
    )
    if not ssh:
        store_outputs(server_alias, job_id, inferred,
                      {"probed": [], "from_workdir": [], "from_slurm_log": []})
        return True

    probe_result = probe_and_collect(
        ssh,
        inferred_paths=[e["path"] for e in inferred],
        workdir=work_dir or "",
        submit_time=float(submit_time or 0),
        slurm_out_path=slurm_out_path,
    )

    # Eligible files: everything we observed to exist.
    candidates = []
    for p in probe_result.get("probed", []):
        if p.get("exists"):
            candidates.append(p)
    candidates += probe_result.get("from_workdir", [])
    candidates += [p for p in probe_result.get("from_slurm_log", [])
                   if p.get("exists")]
    locals_ = download_small_files(
        ssh, server_alias, job_id, candidates, _max_download_bytes()
    )

    store_outputs(server_alias, job_id, inferred, probe_result, locals_)
    return True
