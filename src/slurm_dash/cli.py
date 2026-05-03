import argparse
import re
import shlex
import sys
import textwrap
import time
from pathlib import Path

from .config import (
    load_config, CONFIG_PATH, CACHE_DIR, DB_PATH, init_env, get_db_connection,
)
from .remote_manager import run_ssh, run_scp, RemoteManagerError
from .ui.tui import run_tui

# Wrapper script template — __REAL_SBATCH__ is replaced at install time with
# the resolved path of the real sbatch binary on the remote.
_SBATCH_WRAPPER_TEMPLATE = r"""#!/bin/bash
export SLURM_TRACKER_MAX_MB="${SLURM_TRACKER_MAX_MB:-10}"
stage=""
# Refuse to snapshot $HOME or filesystem roots — a full copy there hangs.
case "$PWD" in
    "$HOME"|"$HOME/"|/|"") ;;
    *)
        mkdir -p "$HOME/.slurm_tracker/staging" 2>/dev/null
        stage=$(mktemp -d "$HOME/.slurm_tracker/staging/XXXXXX" 2>/dev/null) || stage=""
        if [ -n "$stage" ]; then
            cp --reflink=always -a "$PWD"/. "$stage"/ 2>/dev/null \
                || cp -al "$PWD"/. "$stage"/ 2>/dev/null \
                || { rm -rf "$stage"; stage=""; }
        fi
        ;;
esac
_output=$(__REAL_SBATCH__ "$@")
_rc=$?
echo "$_output"
_job_id=$(echo "$_output" | grep -oE '[0-9]+' | tail -n 1)
if [ -n "$_job_id" ]; then
    ~/.slurm_tracker/capture.py "$_job_id" "$stage" "$PWD" "$@" >/dev/null 2>&1 &
elif [ -n "$stage" ]; then
    rm -rf "$stage"
fi
exit $_rc
"""


def _inject_interceptor_cmd():
    # Runs on the remote via python3 -c. Finds the real sbatch binary, writes
    # a standalone wrapper script at ~/.slurm_tracker/bin/sbatch (so it works
    # from bash scripts and subprocesses, not just interactive shells), and
    # prepends that dir to PATH in rc files.
    #
    # The wrapper template is embedded as a repr() literal so that textwrap.dedent
    # works correctly (bash lines at column 0 would otherwise defeat dedent).
    python_script = textwrap.dedent(f"""
        import os, stat
        home = os.path.expanduser('~')
        bin_dir = os.path.join(home, '.slurm_tracker', 'bin')
        os.makedirs(bin_dir, exist_ok=True)

        path_dirs = [d for d in os.environ.get('PATH', '').split(':')
                     if d and d != bin_dir]
        real_sbatch = next(
            (os.path.join(d, 'sbatch') for d in path_dirs
             if os.path.isfile(os.path.join(d, 'sbatch'))
             and os.access(os.path.join(d, 'sbatch'), os.X_OK)),
            'sbatch'
        )

        wrapper = {repr(_SBATCH_WRAPPER_TEMPLATE)}.replace('__REAL_SBATCH__', real_sbatch)

        wrapper_path = os.path.join(bin_dir, 'sbatch')
        with open(wrapper_path, 'w') as f:
            f.write(wrapper)
        os.chmod(wrapper_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                               | stat.S_IROTH | stat.S_IXOTH)

        path_block = (
            '\\n# --- SLURM DASH INTERCEPTOR ---\\n'
            'export PATH="$HOME/.slurm_tracker/bin:$PATH"\\n'
            '# --- END SLURM DASH INTERCEPTOR ---\\n'
        )
        for rc in ['~/.bashrc', '~/.zshrc']:
            rc_path = os.path.expanduser(rc)
            if os.path.isfile(rc_path):
                content = open(rc_path).read()
                if 'SLURM DASH INTERCEPTOR' not in content:
                    with open(rc_path, 'a') as f:
                        f.write(path_block)

        print(f'Wrapper: {{wrapper_path}}')
        print(f'Real sbatch: {{real_sbatch}}')
    """).strip()
    return f"python3 -c {shlex.quote(python_script)}"


def _init_remote_db():
    return textwrap.dedent("""
        import os, sqlite3
        d = os.path.expanduser('~/.slurm_tracker')
        os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(os.path.join(d, '.slurm_tracker.db'), timeout=60)
        c.execute('PRAGMA journal_mode=WAL;')
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            job_id TEXT, array_base_id TEXT, array_task_id TEXT,
            submit_time REAL, work_dir TEXT, snapshot_path TEXT,
            git_hash TEXT, git_diff TEXT,
            submit_script TEXT, submit_argv TEXT,
            env_vars TEXT,
            is_deleted INTEGER DEFAULT 0, updated_at REAL
        )''')
        for col in ('submit_script', 'submit_argv', 'env_vars'):
            try: c.execute(f'ALTER TABLE jobs ADD COLUMN {col} TEXT')
            except sqlite3.OperationalError: pass
        c.commit()
    """).strip()


def add_server(host, alias):
    print(f"Deploying slurm-dash payload to {host}...")
    try:
        run_ssh(host, "mkdir -p ~/.slurm_tracker/snapshots ~/.slurm_tracker/staging")

        package_dir = Path(__file__).parent
        capture_path = package_dir / "remote_payload" / "capture.py"
        run_scp(capture_path, host, "~/.slurm_tracker/capture.py")
        run_ssh(host, "chmod +x ~/.slurm_tracker/capture.py")

        run_ssh(host, f"python3 -c {shlex.quote(_init_remote_db())}")
        run_ssh(host, _inject_interceptor_cmd())

        config_text = ""
        if CONFIG_PATH.exists():
            config_text = CONFIG_PATH.read_text()
        if f"[servers.{alias}]" not in config_text:
            entry = (
                f'\n[servers.{alias}]\n'
                f'ssh_string = "{host}"\n'
                f'sync_on_startup = true\n'
                f'alias = "{alias}"\n'
            )
            with open(CONFIG_PATH, "a") as f:
                f.write(entry)
            print(f"Success! Server {alias} added. Config updated at {CONFIG_PATH}")
        else:
            print(f"Server {alias} already exists in config. Skipped appending.")
    except RemoteManagerError as e:
        print(f"Error deploying to {host}: {e}")
        sys.exit(1)


def _remove_interceptor_cmd():
    # Awk strips the entire block between the marker lines (inclusive) from
    # any rc file that has it. Idempotent: a second invocation is a no-op.
    snippet = textwrap.dedent(r"""
        for rc in ~/.bashrc ~/.zshrc; do
            if [ -f "$rc" ] && grep -q "SLURM DASH INTERCEPTOR" "$rc"; then
                tmp=$(mktemp) && \
                awk '/# --- SLURM DASH INTERCEPTOR ---/{skip=1} \
                     !skip{print} \
                     /# --- END SLURM DASH INTERCEPTOR ---/{skip=0; next}' \
                     "$rc" > "$tmp" && mv "$tmp" "$rc"
            fi
        done
    """).strip()
    return snippet


def _strip_server_from_config(alias):
    """Remove the [servers.<alias>] section (and its key=value lines) from
    config.toml. Returns True if a section was removed."""
    if not CONFIG_PATH.exists():
        return False
    lines = CONFIG_PATH.read_text().splitlines(keepends=True)
    out = []
    skipping = False
    section_header = f"[servers.{alias}]"
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped == section_header:
            skipping = True
            removed = True
            continue
        if skipping:
            # End the skip when we hit the next section header.
            if stripped.startswith("[") and stripped.endswith("]"):
                skipping = False
                out.append(line)
            # else: drop the line (key=value belonging to the removed section)
            continue
        out.append(line)
    # Tidy: collapse any run of 3+ blank lines into 2.
    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    CONFIG_PATH.write_text(text)
    return removed


def remove_server(host, alias=None, *, purge_local=False, yes=False):
    """Strip the slurm-dash bashrc/zshrc interceptor and delete the remote
    tracker dir. Optionally remove the server from local config and drop
    its rows from the local DB.
    """
    if alias is None:
        alias = _alias_for_host(host) or host

    print(f"Will remove slurm-dash from {host} (alias: {alias}):")
    print("  - strip interceptor from ~/.bashrc and ~/.zshrc")
    print("  - rm -rf ~/.slurm_tracker (remote DB, snapshots, staging)")
    print(f"  - drop [servers.{alias}] from {CONFIG_PATH}")
    if purge_local:
        print(f"  - delete local jobs/outputs rows for alias '{alias}'")
    if not yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("Aborted.")
            return

    try:
        run_ssh(host, _remove_interceptor_cmd())
        print("  - interceptor stripped from rc files (or already absent)")
    except RemoteManagerError as e:
        print(f"  - failed to strip interceptor: {e}")

    try:
        run_ssh(host, "rm -rf ~/.slurm_tracker")
        print("  - removed ~/.slurm_tracker on remote")
    except RemoteManagerError as e:
        print(f"  - failed to remove remote tracker dir: {e}")

    if _strip_server_from_config(alias):
        print(f"  - removed [servers.{alias}] from {CONFIG_PATH}")
    else:
        print(f"  - no [servers.{alias}] section found in {CONFIG_PATH}")

    if purge_local:
        try:
            conn = get_db_connection()
            n_jobs = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE server_alias = ?", (alias,)
            ).fetchone()[0]
            conn.execute("DELETE FROM outputs WHERE server_alias = ?", (alias,))
            conn.execute("DELETE FROM jobs WHERE server_alias = ?", (alias,))
            conn.execute("DELETE FROM tags WHERE server_alias = ?", (alias,))
            conn.commit()
            conn.close()
            print(f"  - dropped {n_jobs} local rows for alias '{alias}'")
        except Exception as e:
            print(f"  - local purge failed: {e}")

    print("Done.")


def _alias_for_host(host):
    config = load_config()
    for alias, info in config.get("servers", {}).items():
        if info.get("ssh_string") == host or alias == host:
            return alias
    return None


def purge_server(host, days):
    print(f"Purging slurm_tracker data older than {days} days on {host}...")
    cutoff = time.time() - days * 86400
    alias = _alias_for_host(host)

    remote_script = textwrap.dedent("""
        import os, sqlite3, sys
        cutoff = float(sys.argv[1])
        db = os.path.expanduser('~/.slurm_tracker/.slurm_tracker.db')
        if not os.path.exists(db):
            db = os.path.expanduser('~/.slurm_tracker.db')
        if os.path.exists(db):
            c = sqlite3.connect(db, timeout=60)
            for (p,) in c.execute('SELECT snapshot_path FROM jobs WHERE submit_time < ?', (cutoff,)).fetchall():
                if p:
                    try: os.remove(p)
                    except OSError: pass
            c.execute('DELETE FROM jobs WHERE submit_time < ?', (cutoff,))
            c.commit()
            c.close()
    """).strip()

    try:
        run_ssh(host, f"python3 -c {shlex.quote(remote_script)} {cutoff}")
        run_ssh(host, f"find ~/.slurm_tracker/snapshots -type f -mtime +{int(days)} -delete")
        run_ssh(host, f"find ~/.slurm_tracker/staging -mindepth 1 -maxdepth 1 -type d -mtime +1 -exec rm -rf {{}} +")
        print("Remote purge complete.")
    except RemoteManagerError as e:
        print(f"Error purging remote: {e}")

    if alias:
        try:
            conn = get_db_connection()
            conn.execute(
                "DELETE FROM jobs WHERE server_alias = ? AND submit_time < ?",
                (alias, cutoff),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Error purging local DB: {e}")

    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.tar.gz"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def dump_db(alias=None, include_deleted=False, limit=20, remote=False):
    if remote:
        config = load_config()
        servers = config.get("servers", {})
        if alias:
            if alias not in servers:
                print(f"No such server: {alias}")
                return
            servers = {alias: servers[alias]}
        remote_script = textwrap.dedent("""
            import os, sqlite3
            db = os.path.expanduser('~/.slurm_tracker/.slurm_tracker.db')
            if not os.path.exists(db):
                db = os.path.expanduser('~/.slurm_tracker.db')
            if not os.path.exists(db):
                print('(no remote DB)')
            else:
                c = sqlite3.connect(db)
                rows = c.execute('SELECT job_id, is_deleted, submit_time, updated_at, work_dir, snapshot_path FROM jobs ORDER BY updated_at DESC').fetchall()
                print(f'rows={len(rows)}')
                for r in rows:
                    print(r)
        """).strip()
        for a, info in servers.items():
            ssh_string = info.get("ssh_string")
            print(f"=== remote:{a} ({ssh_string}) ===")
            if not ssh_string:
                print("  (no ssh_string)")
                continue
            try:
                result = run_ssh(ssh_string, f"python3 -c {shlex.quote(remote_script)}", check=False)
                print(result.stdout.rstrip() or "(empty)")
                if result.stderr.strip():
                    print("STDERR:", result.stderr.strip())
            except RemoteManagerError as e:
                print(f"  ssh error: {e}")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    where = [] if include_deleted else ["is_deleted = 0"]
    params = []
    if alias:
        where.append("server_alias = ?")
        params.append(alias)
    sql = (
        "SELECT server_alias, job_id, array_base_id, is_deleted, "
        "submit_time, updated_at, work_dir, snapshot_path, submit_argv FROM jobs"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    rows = cursor.execute(sql, params).fetchall()
    total = cursor.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    print(f"local DB: {DB_PATH}")
    print(f"total rows: {total}, returned: {len(rows)}")
    cols = ["server_alias", "job_id", "array_base_id", "is_deleted",
            "submit_time", "updated_at", "work_dir", "snapshot_path",
            "sbatch_cmd"]
    print("\t".join(cols))
    import json as _json
    for r in rows:
        argv_json = r[8]
        try:
            argv = _json.loads(argv_json) if argv_json else []
        except (TypeError, ValueError):
            argv = [argv_json] if argv_json else []
        cmd = "sbatch " + shlex.join(str(a) for a in argv) if argv else ""
        out = list(r[:8]) + [cmd]
        print("\t".join("" if v is None else str(v) for v in out))


def main():
    init_env()
    parser = argparse.ArgumentParser(description="Slurm-Dash CLI")
    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser("add", help="Add a new Slurm server")
    add_parser.add_argument("host", help="SSH host string (e.g. user@hpc.edu)")
    add_parser.add_argument("--alias", help="Alias for the server", default=None)

    purge_parser = subparsers.add_parser("purge", help="Purge old remote snapshots")
    purge_parser.add_argument("host", help="SSH host string")
    purge_parser.add_argument("--older-than", type=int, help="Days old", default=30)

    remove_parser = subparsers.add_parser(
        "remove",
        help="Uninstall slurm-dash from a server (strip rc block, delete remote DB)",
    )
    remove_parser.add_argument("host", help="SSH host string or alias")
    remove_parser.add_argument("--alias", help="Alias (inferred from host if omitted)",
                               default=None)
    remove_parser.add_argument("--purge-local", action="store_true",
                               help="Also drop local jobs/outputs/tags rows for this alias")
    remove_parser.add_argument("-y", "--yes", action="store_true",
                               help="Skip the confirmation prompt")

    view_parser = subparsers.add_parser("view", help="Launch the Slurm-Dash UI")

    dump_parser = subparsers.add_parser("dump", help="Dump local DB rows to stdout")
    dump_parser.add_argument("--alias", help="Filter by server alias", default=None)
    dump_parser.add_argument("--all", action="store_true", help="Include soft-deleted rows")
    dump_parser.add_argument("--limit", type=int, default=20)
    dump_parser.add_argument("--remote", action="store_true",
                             help="Dump the remote DB on each server instead of local")

    subparsers.add_parser("help", help="Show this help message and exit")

    args = parser.parse_args()

    if args.command == "add":
        alias = args.alias if args.alias else args.host.split("@")[-1]
        add_server(args.host, alias)
    elif args.command == "purge":
        purge_server(args.host, args.older_than)
    elif args.command == "remove":
        # If `host` is actually an alias, resolve it back to the ssh_string.
        cfg = load_config().get("servers", {})
        host = args.host
        if host in cfg:
            host = cfg[host].get("ssh_string", host)
        remove_server(host, alias=args.alias,
                      purge_local=args.purge_local, yes=args.yes)
    elif args.command == "view":
        run_tui()
    elif args.command == "dump":
        dump_db(args.alias, args.all, args.limit, args.remote)
    elif args.command == "help":
        parser.print_help()
    else:
        if len(sys.argv) == 1:
            run_tui()
        else:
            parser.print_help()

if __name__ == "__main__":
    main()
