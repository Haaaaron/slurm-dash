import subprocess

DEFAULT_TIMEOUT = 30
SSH_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2",
    "-o", "BatchMode=yes",
]


class RemoteManagerError(Exception):
    pass


def run_ssh(host, command, check=True, timeout=DEFAULT_TIMEOUT):
    cmd = ["ssh", *SSH_OPTS, host, command]
    try:
        return subprocess.run(
            cmd, check=check, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RemoteManagerError(f"SSH to {host} timed out after {timeout}s")
    except subprocess.CalledProcessError as e:
        raise RemoteManagerError(f"SSH command failed: {e.stderr}")


def run_scp(local_path, host, remote_path, check=True, timeout=DEFAULT_TIMEOUT):
    cmd = ["scp", *SSH_OPTS, str(local_path), f"{host}:{remote_path}"]
    try:
        return subprocess.run(
            cmd, check=check, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RemoteManagerError(f"SCP to {host} timed out after {timeout}s")
    except subprocess.CalledProcessError as e:
        raise RemoteManagerError(f"SCP command failed: {e.stderr}")
