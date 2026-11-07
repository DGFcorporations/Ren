#!/usr/bin/env bash
# Installs Wren desktop launcher into user applications menu
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.local/share/applications"

mkdir -p "$TARGET_DIR"
cp "$SCRIPT_DIR/wren.desktop" "$TARGET_DIR/wren.desktop"
chmod +x "$TARGET_DIR/wren.desktop"

echo "Wren desktop launcher installed to $TARGET_DIR/wren.desktop"
