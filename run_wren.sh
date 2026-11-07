#!/usr/bin/env bash
# One-click Wren Assistant launcher for Linux
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Launching Wren Everyday Assistant ==="

# Check virtualenv
if [ -f ".venv/bin/python" ]; then
    PYTHON_EXEC=".venv/bin/python"
else
    PYTHON_EXEC="$(which python3)"
fi

# Detect display
if [ -n "${DISPLAY:-}" ]; then
    echo "Desktop environment detected ($DISPLAY). Launching native floating window..."
    exec "$PYTHON_EXEC" -m wren.cli --native "$@"
else
    echo "Headless environment detected. Launching web service on port 8765..."
    exec "$PYTHON_EXEC" -m wren.cli --port 8765 "$@"
fi
