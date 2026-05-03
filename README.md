# Slurm-Dash

A lightweight, hybrid local/remote Slurm job tracking system with a Terminal User Interface (TUI). Slurm-Dash allows you to monitor Slurm jobs submitted on remote HPC clusters right from your local machine, avoiding the need to constantly SSH and run `squeue` or `sacct`.

## Overview & Architecture

Slurm-Dash uses a hybrid architecture to ensure fast, responsive tracking without constant SSH overhead:

1. **Remote Interception**: When you add a server, Slurm-Dash injects a lightweight `sbatch` interceptor into the remote `~/.bashrc` and `~/.zshrc`.
2. **Snapshot & Capture**: Every time you submit a job via `sbatch`, the interceptor instantly snapshots your working directory (using fast reflink/hardlink copies) into `~/.slurm_tracker/staging`. It then asynchronously runs `~/.slurm_tracker/capture.py` to record the job ID, arguments, environment, and snapshot into a remote SQLite database (`~/.slurm_tracker/.slurm_tracker.db`).
3. **Local Syncing**: Your local machine periodically syncs data from the remote SQLite database into a local database (`local_state.db`), managed by `platformdirs`.
4. **Live Polling**: When running the UI, the system dynamically polls `squeue` and `sacct` over SSH to overlay live status (pending, running, completed, CPU/memory/GPU usage) onto the tracked jobs.

### Dependencies
Slurm-Dash is extremely lightweight. It depends only on:
- **`platformdirs`**: For robust, cross-platform configuration and data directory paths.
- **`textual`**: To power the modern, responsive Terminal UI.

## Installation & Setup

1. **Install the package locally**:
   ```bash
   pip install .
   ```
   *(We recommend installing inside an isolated virtual environment)*

2. **Add a Remote Server**:
   To start tracking jobs on a remote cluster, deploy the Slurm-Dash payload to it using the `add` command. This uses SSH and SCP to set up the tracker directories, the remote database, and the interceptor script.
   ```bash
   slurm-dash add user@hpc.cluster.edu --alias mycluster
   ```
   *Note: `--alias` is optional. If omitted, the alias will default to the hostname.*

## Usage

### Launching the Dashboard

To view your jobs, simply run the view command. This launches the Textual-based Terminal UI:

```bash
slurm-dash view
# or simply
slurm-dash
```

Inside the UI, you can see tracked jobs, their statuses, request info, and delete them from your view.

### Viewing Raw Data

If you need to inspect the underlying SQLite data for debugging, use the `dump` command:

```bash
slurm-dash dump
# To filter by a specific server alias
slurm-dash dump --alias mycluster
```

### Managing Remote Storage

The tracked snapshots on the remote server can take up space over time. You can purge jobs older than a certain number of days (default is 30):

```bash
slurm-dash purge user@hpc.cluster.edu --older-than 14
```

## Uninstallation

If you wish to completely remove Slurm-Dash from a remote server, use the `remove` command. This will:
- Strip the `sbatch` interceptor from `~/.bashrc` and `~/.zshrc`.
- Delete the remote `~/.slurm_tracker` directory (containing the DB, snapshots, and staging files).
- Remove the server from your local `config.toml`.

```bash
slurm-dash remove user@hpc.cluster.edu
```

To also drop all locally cached job and output rows associated with that server, include the `--purge-local` flag:

```bash
slurm-dash remove user@hpc.cluster.edu --purge-local
```
