import json
import re
import shlex
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.command import Hit, Provider
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header,
    Label, ListItem, ListView, RichLog, Switch, TabbedContent, TabPane,
)

_ENV_VAR_RE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand_env(text: str, env: dict[str, str]) -> str:
    if not env:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))

    return _ENV_VAR_RE.sub(repl, text)

JOB_HIGHLIGHT_STYLE = "black on yellow"

# SLURM terminal states. Once a job is observed in one of these, the row is
# frozen in the local DB (final_state + companion columns) and skipped on
# subsequent probes. CANCELLED appears as "CANCELLED" or "CANCELLED by N".
_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL",
    "BOOT_FAIL", "DEADLINE", "OUT_OF_MEMORY", "PREEMPTED", "REVOKED",
    "SPECIAL_EXIT",
})


def _is_terminal(state: str) -> bool:
    if not state:
        return False
    return state.split()[0].upper() in _TERMINAL_STATES

from ..config import get_db_connection, load_config
from ..jobs import delete_job
from ..output_inferrer import infer_outputs
from ..output_probe import load_outputs, run_full_probe
from ..remote_manager import run_ssh
from ..slurm_api import get_live_status, _parse_gpus_from_gres
from ..sync_engine import sync_all


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
        return f"{d}d{h:d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _render_progress(used_s: int, limit_s: int | None, state: str, width: int = 20) -> Text:
    if state != "RUNNING" or not limit_s or limit_s <= 0:
        return Text("—", style="dim")
    pct = max(0.0, min(1.0, used_s / limit_s))
    fill = int(round(pct * width))
    if pct >= 0.9:
        color = "red"
    elif pct >= 0.75:
        color = "yellow"
    else:
        color = "green"
    bar = Text()
    bar.append("█" * fill, style=color)
    bar.append("░" * (width - fill), style="dim")
    remaining = max(0, limit_s - used_s)
    bar.append(f" {pct * 100:3.0f}% · {_fmt_duration(remaining)} left")
    return bar


class SlurmDashCommandProvider(Provider):
    async def search(self, query: str):
        matcher = self.matcher(query)
        if matcher.match("Refresh Data"):
            yield Hit(
                1.0,
                matcher.highlight("Refresh Data"),
                lambda: self.app.action_refresh_data(),
                help="Force sync data from remote servers",
            )


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return ""
    n = int(n)
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KB", 1024), ("MB", 1024 ** 2),
                      ("GB", 1024 ** 3), ("TB", 1024 ** 4)):
        if n < div * 1024:
            return f"{n / div:.1f} {unit}"
    return f"{n / 1024 ** 4:.1f} TB"


def _fmt_mtime(t: float | None) -> str:
    if not t:
        return ""
    try:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return ""


class FilesModal(ModalScreen):
    DEFAULT_CSS = """
    FilesModal {
        align: center middle;
    }
    FilesModal > Vertical {
        width: 90%;
        height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    FilesModal TabbedContent { height: 1fr; }
    FilesModal Horizontal#split { height: 1fr; }
    FilesModal ListView#file-list {
        width: 40%;
        border-right: solid $accent;
    }
    FilesModal #viewer {
        width: 1fr;
        padding: 0 1;
    }
    FilesModal RichLog#file-content {
        height: 1fr;
        background: $surface;
    }
    FilesModal #env-row {
        height: 1;
        padding: 0 1;
    }
    FilesModal #env-row Label { width: auto; }
    FilesModal #env-row Switch {
        height: 1;
        border: none;
        padding: 0;
        margin: 0 1;
    }
    FilesModal #output-toolbar {
        height: 3;
        padding: 0 1;
    }
    FilesModal #output-toolbar Button { margin-right: 2; }
    FilesModal #output-toolbar Label { width: 1fr; padding: 1 0; }
    FilesModal #output-split { height: 1fr; }
    FilesModal DataTable#output-table {
        height: 60%;
        border-bottom: solid $accent;
    }
    FilesModal RichLog#output-preview {
        height: 1fr;
        background: $surface;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("c", "copy_path", "Copy path"),
        ("o", "open_local", "Open local copy"),
    ]

    MAX_PREVIEW_BYTES = 256 * 1024

    def __init__(self, alias: str, job_id: str, snapshot_path: str,
                 submit_argv: str | None = None, work_dir: str | None = None,
                 env_vars: str | None = None):
        super().__init__()
        self.alias = alias
        self.job_id = job_id
        self.snapshot_path = snapshot_path
        self.submit_argv = submit_argv
        self.work_dir = work_dir
        self.env_vars_json = env_vars
        self._env_dict: dict[str, str] = {}
        if env_vars:
            try:
                parsed = json.loads(env_vars)
                if isinstance(parsed, dict):
                    self._env_dict = {str(k): str(v) for k, v in parsed.items()}
            except (TypeError, ValueError):
                pass
        self.entries: list[str] = []  # parallel to ListView items; entry is "" for non-file rows
        self._current_path: str | None = None
        self._current_body: str = ""
        self._substitute_env: bool = False
        self._output_rows: list[dict] = []  # parallel to DataTable rows

    def _format_sbatch_cmd(self) -> str:
        if not self.submit_argv:
            return "sbatch (argv not recorded)"
        try:
            args = json.loads(self.submit_argv)
        except (TypeError, ValueError):
            return f"sbatch {self.submit_argv}"
        if not isinstance(args, list):
            return f"sbatch {self.submit_argv}"
        return "sbatch " + shlex.join(str(a) for a in args)

    def compose(self) -> ComposeResult:
        header = Text("Files for job ")
        header.append(self.job_id, style=JOB_HIGHLIGHT_STYLE)
        header.append(f" ({self.alias})")
        cmd_text = Text(self._format_sbatch_cmd(), style="bold")
        if self.job_id:
            cmd_text.highlight_regex(re.escape(self.job_id), style=JOB_HIGHLIGHT_STYLE)
        with Vertical():
            yield Label(header)
            yield Label(cmd_text, id="sbatch-cmd")
            if self.work_dir:
                yield Label(Text(f"cwd: {self.work_dir}", style="dim"), id="cwd-line")
            with TabbedContent(initial="tab-snapshot"):
                with TabPane("Submit snapshot", id="tab-snapshot"):
                    env_count = len(self._env_dict)
                    env_label_text = (
                        f"Substitute env vars ({env_count} captured)"
                        if env_count else "Substitute env vars (none captured)"
                    )
                    with Horizontal(id="env-row"):
                        sw = Switch(value=False, id="env-toggle")
                        if not env_count:
                            sw.disabled = True
                        yield sw
                        yield Label(env_label_text, id="env-toggle-label")
                    with Horizontal(id="split"):
                        yield ListView(id="file-list")
                        with Vertical(id="viewer"):
                            yield Label("(select a file)", id="file-header")
                            yield RichLog(id="file-content", wrap=True,
                                          markup=False, highlight=False)
                with TabPane("Output", id="tab-output"):
                    with Horizontal(id="output-toolbar"):
                        yield Button("Re-probe", id="reprobe", variant="primary")
                        yield Label("", id="probe-status")
                    with Vertical(id="output-split"):
                        yield DataTable(id="output-table",
                                        cursor_type="row", zebra_stripes=True)
                        yield RichLog(id="output-preview", wrap=False,
                                      markup=False, highlight=False)
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._load_files()
        table = self.query_one("#output-table", DataTable)
        table.add_columns("Kind", "Path", "Size", "Modified", "Exists", "Local")
        self._refresh_output_table()

    @work(thread=True)
    def _load_files(self) -> None:
        config = load_config()
        ssh_string = (
            config.get("servers", {}).get(self.alias, {}).get("ssh_string")
        )
        files: list[str] = []
        if ssh_string and self.snapshot_path:
            cmd = f"tar -tzf {shlex.quote(self.snapshot_path)}"
            try:
                result = run_ssh(ssh_string, cmd, check=False)
                if result.returncode == 0:
                    files = [f for f in result.stdout.splitlines() if f]
                else:
                    files = [f"(failed: {result.stderr.strip() or 'unknown error'})"]
            except Exception as e:
                files = [f"(error: {e})"]
        else:
            files = ["(no ssh_string or snapshot_path)"]

        def _populate():
            lv = self.query_one("#file-list", ListView)
            lv.clear()
            self.entries = []
            pattern = re.escape(self.job_id) if self.job_id else ""
            for f in files:
                is_file = bool(f) and not f.endswith("/") and not f.startswith("(")
                self.entries.append(f if is_file else "")
                label_text = Text(f)
                if pattern:
                    label_text.highlight_regex(pattern, style=JOB_HIGHLIGHT_STYLE)
                lv.append(ListItem(Label(label_text)))

        self.app.call_from_thread(_populate)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None or idx >= len(self.entries):
            return
        path = self.entries[idx]
        if not path:
            return
        self._current_path = path
        self._fetch_file(path)

    @work(thread=True)
    def _fetch_file(self, path: str) -> None:
        config = load_config()
        ssh_string = (
            config.get("servers", {}).get(self.alias, {}).get("ssh_string")
        )
        body: str
        if not ssh_string or not self.snapshot_path:
            body = "(no ssh_string or snapshot_path)"
        else:
            # Stream the single member out of the tar over SSH; cap at MAX_PREVIEW_BYTES.
            cap = self.MAX_PREVIEW_BYTES
            cmd = (
                f"tar -xzOf {shlex.quote(self.snapshot_path)} "
                f"{shlex.quote(path)} | head -c {cap + 1}"
            )
            try:
                result = run_ssh(ssh_string, cmd, check=False)
                if result.returncode != 0:
                    body = f"(extract failed: {result.stderr.strip() or 'unknown error'})"
                else:
                    raw = result.stdout
                    if "\x00" in raw[:4096]:
                        body = "(binary file — preview suppressed)"
                    elif len(raw.encode("utf-8", "replace")) > cap:
                        body = raw[:cap] + f"\n\n... (truncated at {cap} bytes)"
                    else:
                        body = raw
            except Exception as e:
                body = f"(error: {e})"

        self.app.call_from_thread(self._apply_body, path, body)

    def _apply_body(self, path: str, body: str) -> None:
        self._current_path = path
        self._current_body = body
        self._render_current()

    def _render_current(self) -> None:
        path = self._current_path or "(select a file)"
        body = self._current_body
        if self._substitute_env and self._env_dict:
            body = _expand_env(body, self._env_dict)
        header_text = Text(path)
        if self.job_id:
            header_text.highlight_regex(re.escape(self.job_id), style=JOB_HIGHLIGHT_STYLE)
        try:
            self.query_one("#file-header", Label).update(header_text)
            rl = self.query_one("#file-content", RichLog)
            rl.clear()
            content = Text(body)
            if self.job_id:
                content.highlight_regex(re.escape(self.job_id), style=JOB_HIGHLIGHT_STYLE)
            rl.write(content)
        except Exception:
            # Modal not fully mounted yet; ignore.
            pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "env-toggle":
            self._substitute_env = bool(event.value)
            if self._current_path:
                self._render_current()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss()
        elif event.button.id == "reprobe":
            self._set_probe_status("Probing remote outputs...")
            self._reprobe()

    # --- Output tab --------------------------------------------------

    def _set_probe_status(self, msg: str, *, error: bool = False) -> None:
        try:
            label = self.query_one("#probe-status", Label)
        except Exception:
            return
        style = "red" if error else "dim"
        label.update(Text(msg, style=style))

    def _refresh_output_table(self) -> None:
        rows = load_outputs(self.alias, self.job_id)
        self._output_rows = rows
        try:
            table = self.query_one("#output-table", DataTable)
        except Exception:
            return
        table.clear()
        if not rows:
            self._set_probe_status(
                "No outputs recorded yet — press Re-probe to run the inferrer + SSH probe."
            )
            return
        any_probed = any(r.get("probed_at") for r in rows)
        for r in rows:
            kind_text = Text(r["kind"], style=_KIND_STYLES.get(r["kind"], ""))
            exists = r.get("exists_remote")
            if r.get("probed_at"):
                exists_text = (
                    Text("yes", style="green") if exists
                    else Text("missing", style="red")
                )
            else:
                exists_text = Text("?", style="dim")
            local = r.get("local_path")
            if local:
                local_text = Text("synced", style="green")
            elif exists and not r.get("is_dir"):
                local_text = Text("remote-only", style="dim")
            else:
                local_text = Text("")
            table.add_row(
                kind_text,
                r["path"],
                _fmt_bytes(r.get("size_bytes")),
                _fmt_mtime(r.get("mtime")),
                exists_text,
                local_text,
            )
        if any_probed:
            synced = sum(1 for r in rows if r.get("local_path"))
            self._set_probe_status(
                f"{len(rows)} entries · {synced} synced locally · "
                f"last probe "
                f"{_fmt_mtime(max((r.get('probed_at') or 0) for r in rows))} "
                f"·  c=copy path  o=open local"
            )
        else:
            self._set_probe_status(
                f"{len(rows)} inferred entries (not yet probed) — press Re-probe."
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "output-table":
            return
        idx = event.cursor_row
        if idx is None or idx >= len(self._output_rows):
            return
        row = self._output_rows[idx]
        try:
            preview = self.query_one("#output-preview", RichLog)
        except Exception:
            return
        preview.clear()
        meta = Text()
        meta.append(f"{row['kind']}  ", style="bold")
        meta.append(f"{row['path']}\n", style="cyan")
        if row.get("probed_at"):
            if row.get("exists_remote"):
                meta.append(
                    f"size={_fmt_bytes(row.get('size_bytes'))}  "
                    f"mtime={_fmt_mtime(row.get('mtime'))}  "
                    f"is_dir={'yes' if row.get('is_dir') else 'no'}\n",
                    style="dim",
                )
            else:
                meta.append("(does not exist on remote)\n", style="red")
        else:
            meta.append("(not yet probed)\n", style="dim")
        local = row.get("local_path")
        if local:
            meta.append(f"local: {local}\n", style="green")
        preview.write(meta)
        # Prefer the locally-synced copy if present.
        body = self._read_local_text(local) if local else None
        if body is None:
            body = row.get("head_text")
        if body:
            preview.write(Text(body))

    def _read_local_text(self, local_path: str) -> str | None:
        try:
            with open(local_path, "rb") as f:
                raw = f.read(self.MAX_PREVIEW_BYTES)
        except OSError:
            return None
        if b"\x00" in raw[:4096]:
            return "(binary file — preview suppressed)"
        return raw.decode("utf-8", errors="replace")

    def _selected_output_row(self) -> dict | None:
        try:
            table = self.query_one("#output-table", DataTable)
        except Exception:
            return None
        idx = table.cursor_row
        if idx is None or idx >= len(self._output_rows):
            return None
        return self._output_rows[idx]

    def action_copy_path(self) -> None:
        row = self._selected_output_row()
        if not row:
            self.app.notify("No output row selected.", severity="warning")
            return
        path = row["path"]
        try:
            self.app.copy_to_clipboard(path)
        except Exception:
            pass
        self.app.notify(f"Copied: {path}", title="Path copied", timeout=3)

    def action_open_local(self) -> None:
        row = self._selected_output_row()
        if not row:
            self.app.notify("No output row selected.", severity="warning")
            return
        local = row.get("local_path")
        if not local:
            self.app.notify(
                "No local copy — file is too large or wasn't synced. "
                "Use 'c' to copy the remote path.",
                severity="warning",
            )
            return
        try:
            self.app.copy_to_clipboard(local)
        except Exception:
            pass
        self.app.notify(f"Local: {local}", title="Local path copied", timeout=3)

    @work(thread=True)
    def _reprobe(self) -> None:
        try:
            ok = run_full_probe(self.alias, self.job_id)
        except Exception as e:
            self.app.call_from_thread(self._set_probe_status,
                                      f"Probe failed: {e}", error=True)
            return
        if not ok:
            self.app.call_from_thread(self._set_probe_status,
                                      "Job not found in local DB.", error=True)
            return
        self.app.call_from_thread(self._refresh_output_table)


_KIND_STYLES = {
    "slurm-out": "bold green",
    "slurm-err": "bold red",
    "inferred-redirect": "yellow",
    "from-slurm-log": "cyan",
    "from-workdir": "magenta",
}


class SqueueModal(ModalScreen):
    DEFAULT_CSS = """
    SqueueModal { align: center middle; }
    SqueueModal > Vertical {
        width: 95%;
        height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 1 2;
    }
    SqueueModal DataTable { height: 1fr; }
    SqueueModal #squeue-summary { padding: 0 1; }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, alias: str):
        super().__init__()
        self.alias = alias

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(f"squeue --me · {self.alias}", style="bold"))
            yield Label(Text("(loading...)", style="dim"), id="squeue-summary")
            dt = DataTable(id="squeue-table")
            dt.cursor_type = "row"
            dt.zebra_stripes = True
            dt.add_column("Job ID", width=12)
            dt.add_column("State", width=10)
            dt.add_column("Partition", width=12)
            dt.add_column("Name", width=22)
            dt.add_column("CPUs", width=5)
            dt.add_column("GPUs", width=5)
            dt.add_column("Node / Reason", width=22)
            dt.add_column("Time", width=20)
            dt.add_column("Progress", width=40)
            yield dt
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        config = load_config()
        ssh_string = (
            config.get("servers", {}).get(self.alias, {}).get("ssh_string")
        )
        rows: list[dict] = []
        err = ""
        if not ssh_string:
            err = "no ssh_string configured"
        else:
            cmd = "squeue --me --noheader --format='%i|%T|%P|%j|%M|%l|%R|%C|%b'"
            try:
                result = run_ssh(ssh_string, cmd, check=False, timeout=10)
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        parts = line.split("|")
                        if len(parts) >= 9:
                            rows.append({
                                "job_id": parts[0],
                                "state": parts[1],
                                "partition": parts[2],
                                "name": parts[3],
                                "time_used": parts[4],
                                "time_limit": parts[5],
                                "node_or_reason": parts[6],
                                "cpus": parts[7],
                                "gres": parts[8],
                            })
                else:
                    err = result.stderr.strip() or "squeue failed"
            except Exception as e:
                err = str(e)
        self.app.call_from_thread(self._populate, rows, err)

    def _populate(self, rows: list[dict], err: str) -> None:
        running = sum(1 for r in rows if r["state"] == "RUNNING")
        pending = sum(1 for r in rows if r["state"] == "PENDING")
        summary = Text()
        summary.append(f"{running} running", style="green" if running else "dim")
        summary.append(" · ")
        summary.append(f"{pending} pending", style="yellow" if pending else "dim")
        summary.append(f" · {len(rows)} total")
        if err:
            summary.append(f"  [error: {err}]", style="red")
        self.query_one("#squeue-summary", Label).update(summary)

        dt = self.query_one("#squeue-table", DataTable)
        dt.clear()
        rows_sorted = sorted(
            rows,
            key=lambda r: (r["state"] != "RUNNING", r["state"], r["job_id"]),
        )
        for r in rows_sorted:
            used_s = _parse_slurm_time(r["time_used"]) or 0
            limit_s = _parse_slurm_time(r["time_limit"])
            time_text = Text(f"{r['time_used']} / {r['time_limit']}")
            bar_text = _render_progress(used_s, limit_s, r["state"])
            gpus = _parse_gpus_from_gres(r["gres"]) or 0
            state_style = {
                "RUNNING": "green",
                "PENDING": "yellow",
                "COMPLETING": "cyan",
                "FAILED": "red",
                "CANCELLED": "red",
            }.get(r["state"], "dim")
            dt.add_row(
                r["job_id"],
                Text(r["state"], style=state_style),
                r["partition"],
                r["name"],
                r["cpus"],
                str(gpus),
                r["node_or_reason"],
                time_text,
                bar_text,
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss()


class UsageLabel(Label):
    DEFAULT_CSS = """
    UsageLabel { background: $boost; }
    UsageLabel:hover { background: $accent 40%; }
    """

    def __init__(self, alias: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.alias = alias

    def on_click(self) -> None:
        self.app.push_screen(SqueueModal(self.alias))


def _parse_mem_gb(s: str) -> float:
    if not s:
        return 0.0
    s = s.strip().rstrip("cn")
    if not s:
        return 0.0
    unit = s[-1].upper()
    try:
        val = float(s[:-1]) if unit in "KMGT" else float(s)
    except ValueError:
        return 0.0
    if unit == "K":
        return val / (1024 * 1024)
    if unit == "M":
        return val / 1024
    if unit == "G":
        return val
    if unit == "T":
        return val * 1024
    return val / (1024 ** 3)


class SlurmDashTUI(App):
    CSS = """
    DataTable { height: 1fr; width: 100%; }
    TabPane { padding: 0; }
    UsageLabel.usage {
        height: 1;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("r", "refresh_data", "Refresh"),
        ("d", "delete_job", "Delete"),
        ("f", "show_files", "Files"),
        ("s", "show_squeue", "squeue"),
        ("q", "quit", "Quit"),
    ]

    COMMANDS = App.COMMANDS | {SlurmDashCommandProvider}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # job_id -> (alias, snapshot_path, submit_argv_json, work_dir, env_vars_json)
        self.snapshot_map: dict[
            str, tuple[str, str, str | None, str | None, str | None]
        ] = {}
        # (alias, job_id, snapshot_path, submit_argv_json, work_dir, env_vars_json)
        self.selected_job: tuple[
            str, str, str, str | None, str | None, str | None
        ] | None = None
        self.tables: dict[str, DataTable] = {}
        self.usage_labels: dict[str, "UsageLabel"] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        config = load_config()
        servers = config.get("servers", {})

        if not servers:
            yield Label("No servers configured. Run `slurm-dash add user@host`.")
        elif len(servers) == 1:
            (alias,) = servers.keys()
            usage = self._make_usage_label(alias)
            self.usage_labels[alias] = usage
            yield usage
            dt = self._make_table(alias)
            self.tables[alias] = dt
            yield dt
        else:
            with TabbedContent():
                for alias in servers.keys():
                    with TabPane(alias, id=f"tab-{alias}"):
                        usage = self._make_usage_label(alias)
                        self.usage_labels[alias] = usage
                        yield usage
                        dt = self._make_table(alias)
                        self.tables[alias] = dt
                        yield dt
        yield Footer()

    def _make_usage_label(self, alias: str) -> "UsageLabel":
        return UsageLabel(
            alias,
            Text(f"{alias}: (pending sync) · click for squeue", style="dim"),
            id=f"usage-{alias}",
            classes="usage",
        )

    def _compute_usage_text(self, alias: str, status_map: dict | None) -> Text:
        if status_map is None:
            return Text(f"{alias}: (pending sync)", style="dim")
        running = 0
        pending = 0
        cpus_used = 0
        gpus_used = 0
        mem_used_gb = 0.0
        for info in status_map.values():
            state = (info.get("state") or "").upper()
            if state == "RUNNING":
                running += 1
                try:
                    cpus_used += int(info.get("cpus") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    gpus_used += int(info.get("gpus") or 0)
                except (TypeError, ValueError):
                    pass
                mem_used_gb += _parse_mem_gb(info.get("req_mem") or "")
            elif state == "PENDING":
                pending += 1
        txt = Text()
        txt.append(f"{alias}: ", style="bold")
        txt.append(f"{running} running", style="green" if running else "dim")
        txt.append(
            f" ({cpus_used} CPUs, {gpus_used} GPUs, {mem_used_gb:.1f} GB)"
        )
        txt.append(" · ")
        txt.append(f"{pending} pending", style="yellow" if pending else "dim")
        txt.append(f" · {len(status_map)} known")
        return txt

    def _make_table(self, alias: str) -> DataTable:
        dt = DataTable(id=f"table-{alias}")
        dt.cursor_type = "row"
        dt.zebra_stripes = True
        dt.add_column("Job ID", width=12)
        dt.add_column("Array", width=8)
        dt.add_column("State", width=14)
        dt.add_column("Submit Time", width=20)
        dt.add_column("Work Dir", width=40)
        dt.add_column("CPUs", width=6)
        dt.add_column("Req Mem", width=10)
        dt.add_column("Max RSS", width=10)
        dt.add_column("Nodes", width=24)
        dt.add_column("Git Hash", width=10)
        return dt

    def on_mount(self) -> None:
        self.title = "Slurm-Dash"
        if self.tables:
            self.sub_title = ", ".join(self.tables.keys())
        # Local DB first (instant), then refresh from remote (slow / may fail).
        for alias, table in self.tables.items():
            self._render_local(alias, table, status_map=None)
        self.background_refresh()

    def _render_local(self, alias: str, table: DataTable, status_map: dict | None) -> None:
        usage = self.usage_labels.get(alias)
        if usage is not None:
            usage.update(self._compute_usage_text(alias, status_map))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT job_id, array_base_id, submit_time, work_dir, git_hash,
                   snapshot_path, submit_argv, env_vars,
                   final_state, final_cpus, final_req_mem, final_max_rss,
                   final_gpus, final_node_list
            FROM jobs
            WHERE server_alias = ? AND is_deleted = 0
            ORDER BY submit_time DESC
            """,
            (alias,),
        )
        local_rows = cursor.fetchall()
        conn.close()

        # Persist any jobs newly observed in a terminal state so we stop
        # probing them. Done in one transaction after the read.
        if status_map:
            self._finalize_terminal(alias, status_map, local_rows)

        table.clear()
        for row in local_rows:
            job_id = row[0]
            final_state = row[8]
            self.snapshot_map[job_id] = (alias, row[5], row[6], row[3], row[7])
            dt_str = "N/A"
            if row[2]:
                dt_str = datetime.fromtimestamp(row[2]).strftime('%Y-%m-%d %H:%M:%S')

            if final_state:
                # Frozen row: trust DB, ignore status_map.
                state = final_state
                cpus = row[9] or "N/A"
                req_mem = row[10] or "N/A"
                max_rss = row[11] or ""
                node_list = row[13] or ""
            else:
                s_info = (status_map or {}).get(job_id, {})
                # status_map is None before the first refresh; otherwise a
                # non-finalized job missing from squeue+sacct has aged out
                # of the lookback window — presume done.
                default_state = "(pending sync)" if status_map is None else "COMPLETED"
                state = s_info.get("state", default_state)
                cpus = s_info.get("cpus", "N/A")
                req_mem = s_info.get("req_mem", "N/A")
                max_rss = s_info.get("max_rss", "")
                node_list = s_info.get("node_list", "") or ""

            table.add_row(
                job_id or "N/A",
                row[1] or "N/A",
                state,
                dt_str,
                row[3] or "N/A",
                cpus,
                req_mem,
                max_rss,
                node_list,
                (row[4] or "N/A")[:7],
            )
        table.refresh(layout=True)

    def _finalize_terminal(self, alias: str, status_map: dict, local_rows) -> None:
        # local_rows: tuples whose [0] is job_id and [8] is final_state.
        already_final = {r[0] for r in local_rows if r[8]}
        updates = []
        for job_id, info in status_map.items():
            if job_id in already_final:
                continue
            state = info.get("state") or ""
            if not _is_terminal(state):
                continue
            updates.append((
                state,
                info.get("cpus") or None,
                info.get("req_mem") or None,
                info.get("max_rss") or None,
                int(info.get("gpus") or 0),
                info.get("node_list") or None,
                alias, job_id,
            ))
        if not updates:
            return
        try:
            conn = get_db_connection()
            conn.executemany(
                """
                UPDATE jobs
                SET final_state = ?, final_cpus = ?, final_req_mem = ?,
                    final_max_rss = ?, final_gpus = ?, final_node_list = ?
                WHERE server_alias = ? AND job_id = ?
                """,
                updates,
            )
            conn.commit()
            conn.close()
            # Reflect the newly written values in local_rows so the immediate
            # render below uses the frozen state.
            written = {(u[6], u[7]): u for u in updates}
            for i, r in enumerate(local_rows):
                key = (alias, r[0])
                if key in written:
                    u = written[key]
                    local_rows[i] = (
                        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                        u[0], u[1], u[2], u[3], u[4], u[5],
                    )
        except Exception:
            pass

        # Kick off output discovery + probe for each newly-finalized job.
        # Done on a worker thread; result lands in the `outputs` table.
        for u in updates:
            self._probe_outputs_async(alias, u[7], u[5])

    @work(thread=True)
    def _probe_outputs_async(self, alias: str, job_id: str,
                             final_node_list: str | None) -> None:
        try:
            run_full_probe(alias, job_id)
        except Exception:
            pass

    def _oldest_submit_time(self, alias: str) -> float | None:
        try:
            conn = get_db_connection()
            row = conn.execute(
                "SELECT MIN(submit_time) FROM jobs "
                "WHERE server_alias = ? AND is_deleted = 0 "
                "AND (final_state IS NULL OR final_state = '')",
                (alias,),
            ).fetchone()
            conn.close()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    @work(thread=True)
    def background_refresh(self) -> None:
        self.call_from_thread(self.notify, "Syncing from remote...", title="Syncing")
        try:
            sync_all()
        except Exception as e:
            self.call_from_thread(self.notify, f"Sync failed: {e}", severity="error")

        for alias, table in self.tables.items():
            try:
                status_map = get_live_status(alias, since=self._oldest_submit_time(alias))
            except Exception:
                status_map = {}
            self.call_from_thread(self._render_local, alias, table, status_map)
        self.call_from_thread(self.notify, "Refresh complete.", title="Done")

    def action_refresh_data(self) -> None:
        self.background_refresh()

    def action_show_files(self) -> None:
        if self.selected_job is None:
            self.notify("Select a job first.", severity="warning")
            return
        alias, job_id, snapshot, argv, work_dir, env_vars = self.selected_job
        self.push_screen(FilesModal(alias, job_id, snapshot, argv, work_dir, env_vars))

    def _active_alias(self) -> str | None:
        if len(self.tables) == 1:
            return next(iter(self.tables.keys()))
        try:
            tabs = self.query_one(TabbedContent)
            active_id = tabs.active
            if active_id and active_id.startswith("tab-"):
                return active_id[4:]
        except Exception:
            pass
        return None

    def action_show_squeue(self) -> None:
        alias = self._active_alias()
        if not alias:
            self.notify("No active server.", severity="warning")
            return
        self.push_screen(SqueueModal(alias))

    def action_delete_job(self) -> None:
        if self.selected_job is None:
            self.notify("Select a job first.", severity="warning")
            return
        alias = self.selected_job[0]
        job_id = self.selected_job[1]
        self._do_delete(alias, job_id)

    @work(thread=True)
    def _do_delete(self, alias: str, job_id: str) -> None:
        ok, msg = delete_job(alias, job_id)
        severity = "information" if ok else "error"
        self.call_from_thread(self.notify, f"Job {job_id}: {msg}", severity=severity)
        table = self.tables.get(alias)
        if table is not None:
            try:
                status_map = get_live_status(alias, since=self._oldest_submit_time(alias))
            except Exception:
                status_map = {}
            self.call_from_thread(self._render_local, alias, table, status_map)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        job_id_cell = event.data_table.get_cell_at((event.cursor_row, 0))
        if job_id_cell and str(job_id_cell) in self.snapshot_map:
            alias, snapshot_path, argv, work_dir, env_vars = self.snapshot_map[str(job_id_cell)]
            self.selected_job = (
                alias, str(job_id_cell), snapshot_path or "", argv, work_dir, env_vars,
            )
            self.push_screen(FilesModal(
                alias, str(job_id_cell), snapshot_path or "", argv, work_dir, env_vars,
            ))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            job_id_cell = event.data_table.get_cell_at((event.cursor_row, 0))
        except Exception:
            return
        key = str(job_id_cell) if job_id_cell else None
        if key and key in self.snapshot_map:
            alias, snapshot_path, argv, work_dir, env_vars = self.snapshot_map[key]
            self.selected_job = (alias, key, snapshot_path or "", argv, work_dir, env_vars)


def run_tui():
    app = SlurmDashTUI()
    app.run()
