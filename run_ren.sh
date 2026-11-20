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
pip install -e ".[dev,desktop]"

# The native orb needs GTK3/WebKit2 Python bindings. pip cannot build
# PyGObject/pycairo here, so link the system bindings (python3-gi) into the venv.
SP="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
if ! .venv/bin/python -c 'import gi, cairo' >/dev/null 2>&1 && [ -d /usr/lib/python3/dist-packages ]; then
  echo "/usr/lib/python3/dist-packages" > "$SP/system-gi.pth"
fi
python -m playwright install chromium || echo "Playwright browser install failed. Browser tools will report that when used."

echo "Ren orb: http://127.0.0.1:${PORT:-8765}"
echo "Other agents: ren-mcp   (REN_URL=http://127.0.0.1:${PORT:-8765})"
exec wren "$@"
