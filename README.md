# Slurm-Dash

A lightweight, hybrid local/remote Slurm job tracking system with a Terminal User Interface (TUI). Monitor jobs submitted on remote HPC clusters from your local machine — no constant SSH, no `squeue` polling by hand.

## How It Works

Slurm-Dash uses a three-layer architecture:

1. **Remote wrapper**: `slurm-dash add` deploys a standalone `sbatch` wrapper script to `~/.slurm_tracker/bin/sbatch` on the remote and prepends that directory to `PATH` in `~/.bashrc` and `~/.zshrc`. Because it is a real script on `PATH` (not a shell function), it intercepts `sbatch` calls from interactive shells, bash scripts, Python subprocesses, and workflow managers like Nextflow and Snakemake alike.

2. **Snapshot & capture**: Every `sbatch` call triggers an instant snapshot of the working directory (reflink or hardlink — no byte copies) into a staging area, then asynchronously runs `~/.slurm_tracker/capture.py` to record the job ID, submit arguments, git state, environment, and snapshot into a remote SQLite database at `~/.slurm_tracker/.slurm_tracker.db`.

3. **Local sync & live polling**: Your local machine syncs the remote SQLite database into a local one on demand. When the TUI is open, it polls `squeue` and `sacct` over SSH to overlay live status (pending, running, completed, CPU/memory/GPU) onto the tracked jobs.

## Installation

```bash
curl -LsSf https://raw.githubusercontent.com/haaaaron/slurm-dash/main/install.sh | bash
```

This installs [uv](https://github.com/astral-sh/uv) if it is not already present, then uses it to install slurm-dash into an isolated tool environment. `uv` can bootstrap Python itself, so there are no external dependencies.

After install, make sure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc or ~/.zshrc
```

## Setup

Add a remote cluster (runs over SSH/SCP — key-based auth recommended):

```bash
slurm-dash add user@hpc.cluster.edu --alias mycluster
```

`--alias` is optional; it defaults to the hostname. This command:
- Creates `~/.slurm_tracker/{bin,snapshots,staging}` on the remote
- Deploys the `sbatch` wrapper and `capture.py`
- Initialises the remote SQLite database
- Appends `export PATH="$HOME/.slurm_tracker/bin:$PATH"` to `~/.bashrc` and `~/.zshrc`

Source your rc file (or open a new shell) on the remote for the wrapper to take effect:

```bash
source ~/.bashrc
```

## Usage

```bash
slurm-dash          # launch the TUI (syncs all servers on startup)
slurm-dash view     # same as above
slurm-dash dump     # print raw DB rows to stdout
slurm-dash dump --alias mycluster
slurm-dash purge user@hpc.cluster.edu --older-than 14   # remove snapshots older than N days
```

## Uninstallation

```bash
slurm-dash remove user@hpc.cluster.edu
```

Strips the PATH block from `~/.bashrc`/`~/.zshrc`, deletes `~/.slurm_tracker` on the remote, and removes the server from the local config. Add `--purge-local` to also drop all locally cached jobs and outputs for that server.
