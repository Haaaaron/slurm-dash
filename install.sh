#!/usr/bin/env bash
set -euo pipefail

# Install slurm-dash binary.
# Usage: ./install.sh [nightly]
# If run from inside the repo (has Cargo.toml nearby), build from source.
# Otherwise download from GitHub releases (latest or nightly).

RELEASE_CHANNEL="${1:-latest}"
BIN_DIR="$HOME/.local/bin"
BIN_NAME="slurm-dash"

mkdir -p "$BIN_DIR"

# Try to find script directory for local builds (works when directly executed)
SCRIPT_DIR=""
if [[ -n "${0}" && "${0}" != "-bash" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${0}")/.." 2>/dev/null && pwd)" || SCRIPT_DIR=""
fi

# Check if we're in the repo directory
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/app/Cargo.toml" ]]; then
    echo "Building slurm-dash from local source..."
    cargo build --release --manifest-path "$SCRIPT_DIR/app/Cargo.toml"
    cp "$SCRIPT_DIR/app/target/release/slurm-dash" "$BIN_DIR/slurm-dash"
    echo "✓ slurm-dash installed from local build!"
    echo ""
    echo "To get started:"
    echo "  slurm-dash init-config  # Create config template"
    echo "  slurm-dash add user@cluster --alias mycluster"
    echo "  slurm-dash             # Start the daemon and open the web UI"
    exit 0
fi

# Download from GitHub releases
REPO="haaaaron/slurm-dash"
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Detect OS and architecture
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
    linux)
        OS_NAME="linux"
        case "$ARCH" in
            x86_64) ARCH_NAME="x86_64" ;;
            aarch64 | arm64) ARCH_NAME="aarch64" ;;
            *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
        esac
        ;;
    darwin)
        OS_NAME="darwin"
        case "$ARCH" in
            x86_64) ARCH_NAME="x86_64" ;;
            arm64) ARCH_NAME="arm64" ;;
            *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
        esac
        ;;
    *)
        echo "Unsupported OS: $OS"
        exit 1
        ;;
esac

ARTIFACT_NAME="slurm-dash-${OS_NAME}-${ARCH_NAME}"
if [ "$OS_NAME" = "windows" ]; then
    ARTIFACT_NAME="${ARTIFACT_NAME}.exe"
fi

# Get release download URL
if [ "$RELEASE_CHANNEL" = "nightly" ]; then
    RELEASE_URL="https://api.github.com/repos/$REPO/releases/tags/nightly"
else
    RELEASE_URL="https://api.github.com/repos/$REPO/releases/latest"
fi

DOWNLOAD_URL=$(curl -s "$RELEASE_URL" | grep "browser_download_url.*$ARTIFACT_NAME" | cut -d'"' -f4 | head -1)

if [ -z "$DOWNLOAD_URL" ]; then
    echo "Failed to find binary for ${OS_NAME}-${ARCH_NAME} in $RELEASE_CHANNEL release"
    exit 1
fi

echo "Downloading $ARTIFACT_NAME from $RELEASE_CHANNEL release..."
curl -LsSf "$DOWNLOAD_URL" -o "$TEMP_DIR/$BIN_NAME"
chmod +x "$TEMP_DIR/$BIN_NAME"
mv "$TEMP_DIR/$BIN_NAME" "$BIN_DIR/$BIN_NAME"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "✓ slurm-dash installed to: $BIN_DIR/$BIN_NAME"
    echo ""
    echo "To use it, add to your shell rc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
else
    echo "✓ slurm-dash installed successfully!"
fi

echo ""
echo "To get started:"
echo "  slurm-dash init-config  # Create config template"
echo "  slurm-dash add user@cluster --alias mycluster"
echo "  slurm-dash             # Start the daemon and open the web UI"
