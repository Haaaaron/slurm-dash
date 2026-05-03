"""Infer output paths for a job from its submit script + sbatch argv +
captured env_vars, *without* talking to the remote.

Two confidence tiers:
  - "declared": SLURM --output / --error directives. Trustworthy.
  - "inferred": shell redirects (>, >>, tee, 2>, &>) and env-var hints
    matching common output-path conventions. Best-effort, may miss
    or over-report.
"""

from __future__ import annotations

import json
import os
import re
import shlex


# --- SLURM filename pattern substitution ---------------------------------
# Reference: man sbatch, "filename pattern". We support the cheap subset:
# %j, %J, %x, %u, %A, %a, %N, %n, %t, %% (literal %).
def _expand_slurm_pattern(
    pattern: str,
    job_id: str,
    job_name: str,
    user: str,
    array_master: str = "",
    array_task: str = "",
    node: str = "",
) -> str:
    if not pattern:
        return pattern
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c != "%":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(pattern):
            out.append("%")
            break
        # Optional zero-pad width: %4j etc. Read digits, then code.
        width = 0
        while i < len(pattern) and pattern[i].isdigit():
            width = width * 10 + int(pattern[i])
            i += 1
        if i >= len(pattern):
            break
        code = pattern[i]
        i += 1
        repl = {
            "j": job_id, "J": job_id,
            "x": job_name, "u": user,
            "A": array_master or job_id, "a": array_task,
            "N": node, "n": "0", "t": "0",
            "%": "%",
        }.get(code, "%" + code)
        if width and repl.isdigit():
            repl = repl.rjust(width, "0")
        out.append(repl)
    return "".join(out)


# --- env / shell var expansion -------------------------------------------
_ENV_VAR_RE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand_env(text: str, env: dict) -> str:
    if not text or not env:
        return text

    def repl(m):
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return _ENV_VAR_RE.sub(repl, text)


# --- declared output extraction (#SBATCH --output= and argv) -------------
_SBATCH_OUTPUT_RE = re.compile(
    r"^\s*#SBATCH\s+(?:--output(?:=|\s+)|-o\s+)([^\s#]+)", re.MULTILINE
)
_SBATCH_ERROR_RE = re.compile(
    r"^\s*#SBATCH\s+(?:--error(?:=|\s+)|-e\s+)([^\s#]+)", re.MULTILINE
)
_SBATCH_JOBNAME_RE = re.compile(
    r"^\s*#SBATCH\s+(?:--job-name(?:=|\s+)|-J\s+)([^\s#]+)", re.MULTILINE
)


def _argv_get(argv: list, long_name: str, short: str | None = None) -> str | None:
    """Find `--long=VAL` / `--long VAL` / `-s VAL` in an sbatch argv list."""
    for i, a in enumerate(argv):
        if a == long_name or (short and a == short):
            if i + 1 < len(argv):
                return argv[i + 1]
        if a.startswith(long_name + "="):
            return a[len(long_name) + 1:]
        if short and a.startswith(short) and len(a) > len(short) and not a.startswith("--"):
            # e.g. -oslurm.out
            return a[len(short):]
    return None


# --- inferred-output regexes ---------------------------------------------
# Lines with shell redirects. We grep over the script body (after stripping
# #SBATCH directives so we don't double-count).
_REDIRECT_RES = [
    re.compile(r"(?:^|\s)(?:&>|2>&1>|2>|>>|>)\s*([^\s|;&<>][^\s|;&<>]*)"),
    re.compile(r"\|\s*tee\s+(?:-a\s+)?([^\s|;&<>][^\s|;&<>]*)"),
]

def _strip_sbatch_directives(script_text: str) -> str:
    return "\n".join(
        line for line in (script_text or "").splitlines()
        if not re.match(r"^\s*#SBATCH\b", line)
    )


def _abs_path(path: str, work_dir: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    if work_dir:
        return os.path.normpath(os.path.join(work_dir, path))
    return path


def infer_outputs(
    *,
    job_id: str,
    work_dir: str,
    submit_argv_json: str | None,
    submit_script: str | None,
    env_vars_json: str | None,
    final_node_list: str | None = None,
) -> list[dict]:
    """Return a list of {kind, path, source} dicts.

    `kind` is one of: 'slurm-out', 'slurm-err', 'inferred-redirect',
    'inferred-env'. Paths are made absolute against `work_dir` when they
    are relative. SLURM filename codes (%j, %x, ...) are substituted.
    Shell vars ($X, ${X}) are substituted from env_vars when present.
    """
    try:
        argv = json.loads(submit_argv_json) if submit_argv_json else []
    except (TypeError, ValueError):
        argv = []
    try:
        env = json.loads(env_vars_json) if env_vars_json else {}
    except (TypeError, ValueError):
        env = {}

    user = env.get("USER", "")
    script_text = submit_script or ""

    # Extract job name: argv > #SBATCH > script filename
    job_name = (
        _argv_get(argv, "--job-name", "-J")
        or (_SBATCH_JOBNAME_RE.search(script_text).group(1)
            if _SBATCH_JOBNAME_RE.search(script_text) else "")
        or "slurm"
    )
    # First non-flag argv entry is conventionally the script path.
    script_arg = next((a for a in argv if a and not a.startswith("-")), "")

    node = (final_node_list or "").split(",")[0].strip() or ""

    def _expand(pat: str) -> str:
        s = _expand_slurm_pattern(
            pat, job_id=job_id, job_name=job_name, user=user, node=node,
        )
        s = _expand_env(s, env)
        return _abs_path(s, work_dir)

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, path: str, source: str) -> None:
        if not path:
            return
        key = (kind, path)
        if key in seen:
            return
        seen.add(key)
        results.append({"kind": kind, "path": path, "source": source})

    # --- declared (--output / --error) ---
    out_pat = _argv_get(argv, "--output", "-o")
    if out_pat is None:
        m = _SBATCH_OUTPUT_RE.search(script_text)
        out_pat = m.group(1) if m else None
    if out_pat is None:
        # SLURM default: slurm-%j.out in the working dir
        out_pat = "slurm-%j.out"
    add("slurm-out", _expand(out_pat), "slurm --output (default if unset)")

    err_pat = _argv_get(argv, "--error", "-e")
    if err_pat is None:
        m = _SBATCH_ERROR_RE.search(script_text)
        err_pat = m.group(1) if m else None
    if err_pat:
        add("slurm-err", _expand(err_pat), "slurm --error")

    # --- inferred from script body: shell redirects ---
    body = _strip_sbatch_directives(script_text)
    for line in body.splitlines():
        # Strip inline comments (best-effort).
        line_no_comment = re.split(r"\s#", line, maxsplit=1)[0]
        if not line_no_comment.strip() or line_no_comment.strip().startswith("#"):
            continue
        for regex in _REDIRECT_RES:
            for m in regex.finditer(line_no_comment):
                raw = m.group(1).strip().strip('"').strip("'")
                if not raw or raw.startswith("/dev/"):
                    continue
                add("inferred-redirect", _expand(raw),
                    f"redirect: {line.strip()[:80]}")

    return results
