#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/haaaaron/slurm-dash"

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing slurm-dash..."
uv tool install "git+${REPO_URL}"

UV_BIN="$HOME/.local/bin"
if [[ ":$PATH:" != *":$UV_BIN:"* ]]; then
    echo ""
    echo "slurm-dash is installed but not yet on PATH. Add to your shell rc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "Done! Verify with: slurm-dash --version"
