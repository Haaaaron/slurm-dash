#!/bin/bash
set -e

BIN_DIR="$HOME/.local/bin"
BIN_NAME="slurm-dash"
CONFIG_DIR="$HOME/.config/slurm-dash"
DATA_DIR="$HOME/.local/share/slurm-dash"

echo "Uninstalling slurm-dash..."

if [ -f "$BIN_DIR/$BIN_NAME" ]; then
    # Try to stop daemon first
    if command -v "$BIN_DIR/$BIN_NAME" &> /dev/null; then
        "$BIN_DIR/$BIN_NAME" stop 2>/dev/null || true
    fi
    rm -f "$BIN_DIR/$BIN_NAME"
    echo "✓ Removed binary from $BIN_DIR/$BIN_NAME"
else
    echo "Binary not found at $BIN_DIR/$BIN_NAME"
fi

# Prompt to remove config and data
if [ -d "$CONFIG_DIR" ] || [ -d "$DATA_DIR" ]; then
    echo ""
    echo "Remove local data and configuration?"
    echo "  $CONFIG_DIR"
    echo "  $DATA_DIR"
    read -p "Remove? [y/N] " -r
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        [ -d "$CONFIG_DIR" ] && rm -rf "$CONFIG_DIR" && echo "✓ Removed $CONFIG_DIR"
        [ -d "$DATA_DIR" ] && rm -rf "$DATA_DIR" && echo "✓ Removed $DATA_DIR"
    fi
fi

echo ""
echo "✓ slurm-dash uninstalled"
