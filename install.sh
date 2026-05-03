#!/usr/bin/env bash
set -euo pipefail

# If run from inside the repo directory, install from local path (dev/testing).
# Otherwise install from GitHub via SSH (requires SSH key for github.com).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    INSTALL_SRC="$SCRIPT_DIR"
else
    INSTALL_SRC="git+ssh://git@github.com/haaaaron/slurm-dash"
fi

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing slurm-dash from: $INSTALL_SRC"
uv tool install "$INSTALL_SRC" --reinstall

UV_BIN="$HOME/.local/bin"
if [[ ":$PATH:" != *":$UV_BIN:"* ]]; then
    echo ""
    echo "slurm-dash is installed but not yet on PATH. Add to your shell rc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "Done! Verify with: slurm-dash --version"
