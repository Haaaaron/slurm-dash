#!/usr/bin/env python3
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

TRACKER_DIR = Path.home() / ".slurm_tracker"
DB_PATH = TRACKER_DIR / ".slurm_tracker.db"
SNAPSHOTS_DIR = TRACKER_DIR / "snapshots"
MAX_MB = int(os.environ.get('SLURM_TRACKER_MAX_MB', 10))
MAX_BYTES = MAX_MB * 1024 * 1024

JOB_COLUMNS = [
    "job_id", "array_base_id", "array_task_id",
    "submit_time", "work_dir", "snapshot_path",
    "git_hash", "git_diff",
    "submit_script", "submit_argv",
    "env_vars",
    "is_deleted", "updated_at",
]


def init_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
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
            updated_at REAL
        )
    ''')
    for col, decl in (("submit_script", "TEXT"), ("submit_argv", "TEXT"),
                      ("env_vars", "TEXT")):
        try:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def run_cmd(cmd, cwd=None):
    # NB: Python 3.6 compatible (no capture_output=, no text=). Lumi's system
    # python3 is 3.6, and capture.py runs there.
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=15,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


# --- gitignore matching --------------------------------------------------
# Focused gitignore implementation (subset of git's wildmatch). Handles:
# comments, blank lines, leading '!' (negation), trailing '/' (dir-only),
# leading '/' (anchored), '*', '?', '**'. Last-match-wins.

def _compile_pattern(raw):
    pat = raw.rstrip()
    if not pat or pat.startswith("#"):
        return None
    negate = pat.startswith("!")
    if negate:
        pat = pat[1:]
    dir_only = pat.endswith("/")
    if dir_only:
        pat = pat[:-1]
    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    # If pattern contains a slash anywhere, it's anchored relative to root
    if "/" in pat:
        anchored = True
    parts = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                parts.append(".*")
                i += 2
                if i < len(pat) and pat[i] == "/":
                    i += 1
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c in r".+()|^$\\{}[]":
            parts.append(re.escape(c))
            i += 1
        else:
            parts.append(c)
            i += 1
    body = "".join(parts)
    if anchored:
        regex = re.compile(r"^" + body + r"(/.*)?$")
    else:
        regex = re.compile(r"(^|.*/)" + body + r"(/.*)?$")
    return (regex, negate, dir_only)


def load_ignore_patterns(root):
    patterns = []
    for default in (".git/", "__pycache__/", "*.pyc", ".pytest_cache/"):
        p = _compile_pattern(default)
        if p:
            patterns.append(p)
    gi = os.path.join(root, ".gitignore")
    if os.path.isfile(gi):
        try:
            with open(gi, "r", errors="replace") as f:
                for line in f:
                    p = _compile_pattern(line.strip())
                    if p:
                        patterns.append(p)
        except OSError:
            pass
    return patterns


def is_ignored(rel_path, is_dir, patterns):
    rel_path = rel_path.replace(os.sep, "/")
    # Also test ancestor paths: a dir-only pattern matching an ancestor
    # excludes everything beneath it.
    candidates = [(rel_path, is_dir)]
    parts = rel_path.split("/")
    for i in range(1, len(parts)):
        candidates.append(("/".join(parts[:i]), True))
    ignored = False
    for regex, negate, dir_only in patterns:
        for cand, cand_is_dir in candidates:
            if dir_only and not cand_is_dir:
                continue
            if regex.match(cand):
                ignored = not negate
                break
    return ignored


# --- submit-script and array parsing ------------------------------------

def _resolve_script(args, search_dirs):
    """Return (script_path, script_text) for the first arg that resolves
    to an existing regular file under any of search_dirs (or as absolute)."""
    for arg in args:
        if not arg or arg.startswith("-"):
            continue
        candidates = [arg]
        if not os.path.isabs(arg):
            for d in search_dirs:
                if d:
                    candidates.append(os.path.join(d, arg))
        for cand in candidates:
            try:
                if os.path.isfile(cand):
                    with open(cand, "r", errors="replace") as f:
                        return cand, f.read()
            except OSError:
                continue
    return None, ""


_ARRAY_RE = re.compile(r"#SBATCH\s+(?:--array(?:=|\s+)|-a\s+)([^\s#]+)")


def _capture_loaded_modules(env):
    """Return a textual listing of currently-loaded environment modules
    inferred from the parent shell's environment.

    `LOADEDMODULES` and `_LMFILES_` are exported by both Lmod and
    environment-modules and are colon-separated lists that line up index
    by index. We never invoke `module list` itself: it's a shell function
    that's not reliably reachable from a python subprocess.
    """
    loaded = env.get("LOADEDMODULES", "")
    if not loaded:
        return ""
    names = [n for n in loaded.split(":") if n]
    files = (env.get("_LMFILES_", "") or "").split(":")
    lmod_version = env.get("LMOD_VERSION", "")
    mgr = "Lmod" if lmod_version else (
        "environment-modules" if env.get("MODULESHOME") else "unknown")
    out = [f"# manager: {mgr}"]
    if lmod_version:
        out.append(f"# lmod-version: {lmod_version}")
    out.append("")
    width = max((len(n) for n in names), default=0)
    for i, name in enumerate(names):
        path = files[i] if i < len(files) else ""
        if path:
            out.append(f"{name.ljust(width)}  {path}")
        else:
            out.append(name)
    return "\n".join(out) + "\n"


def _has_array_directive(argv, script_text):
    for a in argv:
        if a.startswith("--array=") or a == "--array" or a == "-a":
            return True
    return bool(_ARRAY_RE.search(script_text or ""))


# Match `VAR=...` and `export VAR=...` (single-line). We deliberately do not
# try to handle line continuations — keeps the sandbox surface small.
_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=")


def _eval_script_exports(script_text, job_id, parent_env, positional_args=None):
    """Return a dict of vars defined by assignment/export lines in
    `script_text`, evaluated in a bash subshell that inherits `parent_env`
    plus a synthesized SLURM_JOB_ID. Positional args ($1, $2, ...) from the
    sbatch invocation are forwarded so variables like TEMP=$1 resolve correctly.
    Compute lines (python, srun, mkdir, ...) are filtered out.
    Returns only entries that differ from parent_env."""
    if not script_text:
        return {}
    assign_lines = [
        line for line in script_text.splitlines()
        if _ASSIGN_RE.match(line)
    ]
    if not assign_lines:
        return {}
    env_in = dict(parent_env)
    env_in.setdefault("SLURM_JOB_ID", str(job_id))
    env_in.setdefault("SLURM_JOBID", str(job_id))
    snippet = "set -a\n" + "\n".join(assign_lines) + "\n/usr/bin/env -0\n"
    # Pass positional args so $1, $2, ... resolve inside the script's assignment
    # lines. bash -c 'script' name $1 $2 ... sets $0=name, $1=first-arg, etc.
    pos = list(positional_args) if positional_args else []
    try:
        result = subprocess.run(
            ["bash", "-c", snippet, "sbatch_script", *pos],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env_in, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if result.returncode != 0:
        return {}
    raw = result.stdout.decode("utf-8", errors="replace")
    out = {}
    for entry in raw.split("\0"):
        if "=" not in entry:
            continue
        k, _, v = entry.partition("=")
        out[k] = v
    # Bash auto-sets these in any subshell — they're not user-defined.
    bash_noise = {"_", "SHLVL", "PWD", "OLDPWD"}
    return {
        k: v for k, v in out.items()
        if k not in bash_noise and parent_env.get(k) != v
    }


# --- main ---------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        return

    job_id = sys.argv[1]
    stage_dir = sys.argv[2] if len(sys.argv) >= 3 else ""
    original_cwd = sys.argv[3] if len(sys.argv) >= 4 else os.getcwd()
    sbatch_argv = list(sys.argv[4:])

    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = TRACKER_DIR / "capture.log"

    used_stage = bool(stage_dir) and os.path.isdir(stage_dir)
    walk_dir = stage_dir if used_stage else original_cwd

    try:
        submit_time = time.time()

        git_hash = run_cmd("git rev-parse HEAD", cwd=walk_dir)
        git_diff = run_cmd("git diff", cwd=walk_dir)

        env_txt = run_cmd("pip freeze")
        if not env_txt:
            env_txt = run_cmd("conda env export")

        script_path, script_text = _resolve_script(
            sbatch_argv, [walk_dir, original_cwd]
        )
        array_base_id = job_id if _has_array_directive(sbatch_argv, script_text) else None

        # Find the positional arguments ($1, $2, ...) passed after the script
        # in the sbatch command line, so assignment lines like TEMP=$1 resolve.
        _script_idx = None
        for _i, _arg in enumerate(sbatch_argv):
            if not _arg or _arg.startswith("-"):
                continue
            _search = (
                [_arg] if os.path.isabs(_arg)
                else [_arg, os.path.join(walk_dir, _arg), os.path.join(original_cwd, _arg)]
            )
            if any(os.path.isfile(_c) for _c in _search):
                _script_idx = _i
                break
        positional_args = list(sbatch_argv[_script_idx + 1:]) if _script_idx is not None else []

        # Snapshot the user's shell env at submit time. capture.py inherits
        # the parent shell's environment via the bash sbatch() wrapper.
        env_vars_dict = dict(os.environ)
        # capture.py runs at submit time, so any var defined inside the
        # submit script (e.g. `export OUTPUT_DIR=...`) hasn't been set yet.
        # Evaluate the script's assignment/export lines in a sandbox bash
        # with SLURM_JOB_ID synthesized, then overlay the result so vars
        # like $OUTPUT_DIR / $RUN_TAG end up in the captured env_vars.
        try:
            extra = _eval_script_exports(script_text, job_id, env_vars_dict, positional_args)
            if extra:
                env_vars_dict.update(extra)
        except Exception as e:
            with open(log_file, "a") as f:
                f.write(f"{datetime.utcnow()}: script-export eval failed for {job_id}: {e}\n")
        env_vars_json = json.dumps(env_vars_dict)

        timestamp = int(submit_time)
        snapshot_filename = f"{timestamp}_{job_id}.tar.gz"
        snapshot_path = SNAPSHOTS_DIR / snapshot_filename

        ignore_patterns = load_ignore_patterns(walk_dir)

        try:
            with tarfile.open(snapshot_path, "w:gz") as tar:
                env_file = TRACKER_DIR / f"tmp_env_{job_id}.txt"
                env_file.write_text(env_txt or "")
                tar.add(env_file, arcname="env.txt")
                env_file.unlink()

                if script_text:
                    script_file = TRACKER_DIR / f"tmp_script_{job_id}.sh"
                    script_file.write_text(script_text)
                    tar.add(script_file, arcname="submit_script.sh")
                    script_file.unlink()

                env_file = TRACKER_DIR / f"tmp_envvars_{job_id}.txt"
                env_file.write_text(
                    "\n".join(f"{k}={v}" for k, v in sorted(env_vars_dict.items()))
                )
                tar.add(env_file, arcname="env_vars.txt")
                env_file.unlink()

                # Snapshot loaded environment modules. Pulled from the
                # captured env (which already has script-defined exports
                # overlaid), so a `module load` line in the submit script
                # would be reflected once it actually runs — but at submit
                # time only modules loaded by the parent shell are present.
                modules_text = _capture_loaded_modules(env_vars_dict)
                if modules_text:
                    mod_file = TRACKER_DIR / f"tmp_modules_{job_id}.txt"
                    mod_file.write_text(modules_text)
                    tar.add(mod_file, arcname="modules.txt")
                    mod_file.unlink()

                for root, dirs, files in os.walk(walk_dir):
                    rel_root = os.path.relpath(root, walk_dir)
                    rel_root = "" if rel_root == "." else rel_root
                    dirs[:] = [
                        d for d in dirs
                        if not is_ignored(
                            os.path.join(rel_root, d) if rel_root else d,
                            True, ignore_patterns,
                        )
                    ]
                    for fname in files:
                        rel = os.path.join(rel_root, fname) if rel_root else fname
                        if is_ignored(rel, False, ignore_patterns):
                            continue
                        full = os.path.join(root, fname)
                        try:
                            if (not os.path.islink(full)
                                    and os.path.getsize(full) <= MAX_BYTES):
                                tar.add(full, arcname=rel)
                        except OSError:
                            pass
        except (OSError, tarfile.TarError) as e:
            with open(log_file, "a") as f:
                f.write(f"{datetime.utcnow()}: Tar error for {job_id} - {e}\n")

        conn = init_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (
                job_id, array_base_id, array_task_id,
                submit_time, work_dir, snapshot_path,
                git_hash, git_diff,
                submit_script, submit_argv,
                env_vars,
                is_deleted, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        ''', (
            job_id, array_base_id, None,
            submit_time, original_cwd, str(snapshot_path),
            git_hash, git_diff,
            script_text or None, json.dumps(sbatch_argv),
            env_vars_json,
            time.time(),
        ))
        conn.commit()
        conn.close()

        with open(log_file, "a") as f:
            f.write(f"{datetime.utcnow()}: Successfully captured {job_id}\n")

    except Exception:
        import traceback
        with open(log_file, "a") as f:
            f.write(
                f"{datetime.utcnow()}: Fatal error in capture for {job_id}:\n"
                f"{traceback.format_exc()}\n"
            )
    finally:
        if used_stage:
            shutil.rmtree(stage_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
