#!/bin/bash
set -e

RELEASE_CHANNEL="${1:-latest}"
BIN_DIR="$HOME/.local/bin"
BIN_NAME="slurm-dash"
REPO="haaaaron/slurm-dash"
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

mkdir -p "$BIN_DIR"

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
    linux)
        OS_NAME="linux"
        case "$ARCH" in
            x86_64) ARCH_NAME="x86_64" ;;
            aarch64|arm64) ARCH_NAME="aarch64" ;;
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
[ "$OS_NAME" = "windows" ] && ARTIFACT_NAME="${ARTIFACT_NAME}.exe"

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

echo "✓ slurm-dash installed successfully to $BIN_DIR/$BIN_NAME"

if [ ":$PATH:" != *":$BIN_DIR:"* ]; then
    echo ""
    echo "To use it, add to your shell rc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "To get started:"
echo "  slurm-dash init-config  # Create config template"
echo "  slurm-dash add user@cluster --alias mycluster"
echo "  slurm-dash             # Start the daemon and open the web UI"
