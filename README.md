# Slurm-Dash

A lightweight, hybrid local/remote Slurm job tracking system with a Terminal User Interface (TUI). Monitor jobs submitted on remote HPC clusters from your local machine — no constant SSH, no `squeue` polling by hand.

## How It Works

Slurm-Dash uses a three-layer architecture:

1. **Remote wrapper**: `slurm-dash add` deploys a standalone `sbatch` wrapper script to `~/.slurm_tracker/bin/sbatch` on the remote and prepends that directory to `PATH` in `~/.bashrc` and `~/.zshrc`. Because it is a real script on `PATH` (not a shell function), it intercepts `sbatch` calls from interactive shells, bash scripts, Python subprocesses, and workflow managers like Nextflow and Snakemake alike.

2. **Snapshot & capture**: Every `sbatch` call triggers an instant snapshot of the working directory (reflink or hardlink — no byte copies) into a staging area, then asynchronously runs `~/.slurm_tracker/capture.py` to record the job ID, submit arguments, git state, environment, and snapshot into a remote SQLite database at `~/.slurm_tracker/.slurm_tracker.db`.

3. **Local sync & live polling**: Your local machine syncs the remote SQLite database into a local one on demand. When the TUI is open, it polls `squeue` and `sacct` over SSH to overlay live status (pending, running, completed, CPU/memory/GPU) onto the tracked jobs.

## Installation

Download the prebuilt binary for your OS and architecture:

```bash
curl -LsSf https://raw.githubusercontent.com/haaaaron/slurm-dash/main/install.sh | bash
```

This detects your OS and architecture, downloads the appropriate binary from the latest GitHub release, and installs it to `~/.local/bin/slurm-dash`.

After install, make sure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc or ~/.zshrc
```

Supported platforms:
- Linux (x86_64, aarch64) — fully static musl binaries
- macOS (x86_64, ARM64)
- Windows (x86_64)

## Setup

Initialize a config template:

```bash
slurm-dash init-config
```

Edit `~/.config/slurm-dash/config.toml` and add your cluster(s):

```toml
[servers.mycluster]
ssh_string = "user@hpc.cluster.edu"
```

Or use the one-shot add command:

```bash
slurm-dash add user@hpc.cluster.edu --alias mycluster
```

Either way, `slurm-dash` will auto-install the sbatch interceptor on first startup.

## Usage

Start the daemon and open the web UI:

```bash
slurm-dash
```

(This spawns a background daemon on first run, then opens your browser to http://localhost:8765.)

Other commands:

```bash
slurm-dash serve --port 9000     # foreground mode (e.g., for systemd)
slurm-dash list                  # list configured clusters
slurm-dash status                # check if daemon is running
slurm-dash stop                  # stop the background daemon
slurm-dash add <ssh> --alias <name>     # add a new cluster
slurm-dash remove <alias> --yes  # remove a cluster
slurm-dash remove <alias> --purge-local --yes  # also delete local job records
```

## Uninstallation

```bash
slurm-dash stop                         # stop the daemon
slurm-dash remove <alias> --yes         # remove a cluster
rm ~/.local/bin/slurm-dash              # delete the binary
```
