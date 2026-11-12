#!/usr/bin/env bash
# Start Ren on this workstation. The phone companion and ren-mcp talk to this process.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3.12 >/dev/null 2>&1; then
  PY=python3.12
else
  PY=python3
  echo "Python 3.12 is the version this project is pinned to. Continuing with $($PY -V)."
fi

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium || echo "Playwright browser install failed. Browser tools will report that when used."

echo "Ren orb: http://127.0.0.1:${PORT:-8765}"
echo "Other agents: ren-mcp   (REN_URL=http://127.0.0.1:${PORT:-8765})"
exec wren "$@"
