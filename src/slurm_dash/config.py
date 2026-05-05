import tomllib
import sqlite3
from pathlib import Path
from platformdirs import PlatformDirs

APP_NAME = "slurm-dash"
dirs = PlatformDirs(APP_NAME)

CONFIG_DIR = dirs.user_config_path
DATA_DIR = dirs.user_data_path
CACHE_DIR = dirs.user_cache_path

CONFIG_PATH = CONFIG_DIR / "config.toml"
DB_PATH = DATA_DIR / "local_state.db"

DEFAULT_CONFIG = """[general]
max_download_mb = 500
"""

def init_env():
    """Ensure all directories exist and default config/DB are initialized."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)

    _init_db()

def _init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # jobs table with composite primary key (server_alias, job_id)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            server_alias TEXT,
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
            updated_at REAL,
            final_state TEXT,
            final_cpus TEXT,
            final_req_mem TEXT,
            final_max_rss TEXT,
            final_gpus INTEGER,
            final_gpu_model TEXT,
            final_node_list TEXT,
            output_probed_at REAL,
            PRIMARY KEY (server_alias, job_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS outputs (
            server_alias TEXT,
            job_id TEXT,
            kind TEXT,
            path TEXT,
            size_bytes INTEGER,
            mtime REAL,
            exists_remote INTEGER DEFAULT 0,
            is_dir INTEGER DEFAULT 0,
            head_text TEXT,
            local_path TEXT,
            discovered_at REAL,
            probed_at REAL,
            PRIMARY KEY (server_alias, job_id, path)
        )
    ''')
    for col, decl in (("local_path", "TEXT"),):
        try:
            cursor.execute(f"ALTER TABLE outputs ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    # Best-effort migrations for pre-existing DBs.
    for col, decl in (("submit_script", "TEXT"), ("submit_argv", "TEXT"),
                      ("env_vars", "TEXT"),
                      ("final_state", "TEXT"), ("final_cpus", "TEXT"),
                      ("final_req_mem", "TEXT"), ("final_max_rss", "TEXT"),
                      ("final_gpus", "INTEGER"), ("final_gpu_model", "TEXT"),
                      ("final_node_list", "TEXT"),
                      ("output_probed_at", "REAL")):
        try:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            server_alias TEXT,
            job_id TEXT,
            tag_name TEXT,
            PRIMARY KEY (server_alias, job_id, tag_name)
        )
    ''')
    conn.commit()
    conn.close()

def get_db_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=60)

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        init_env()
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)
