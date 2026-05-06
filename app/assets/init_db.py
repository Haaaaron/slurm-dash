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
