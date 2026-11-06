"""Pinned Streamlit bootstrap with strict loopback policy and isolated empty secrets.

This narrows framework defaults; it does not disable CORS/XSRF or load user config.
No arbitrary Streamlit script or extra CLI flags are accepted.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def streamlit_flags(port: int, state_dir: Path) -> list[str]:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid monitor port")
    return [
        "run",
        str(Path(__file__).with_name("monitor_app.py")),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.allowedHosts=127.0.0.1",
        f"--server.corsAllowedOrigins=http://127.0.0.1:{port}",
        "--browser.serverAddress=127.0.0.1",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--server.fileWatcherType=none",
        "--server.enableCORS=true",
        "--server.enableXsrfProtection=true",
        "--server.enableStaticServing=false",
        "--server.maxUploadSize=1",
        "--server.maxMessageSize=4",
        "--client.toolbarMode=minimal",
        "--logger.level=warning",
        f"--secrets.files={state_dir / 'empty-secrets.toml'}",
    ]


def validate_isolation(state_dir: Path) -> None:
    state_dir = Path(state_dir)
    if not state_dir.is_absolute() or state_dir.is_symlink() or state_dir.resolve() != state_dir:
        raise RuntimeError("Monitor runtime must use its own absolute, non-symlink state directory")
    expected_home, expected_work = state_dir / "home", state_dir / "work"
    if Path(os.environ.get("HOME", "")).resolve() != expected_home or Path.cwd() != expected_work:
        raise RuntimeError("Streamlit home and working directory are not isolated")
    secrets_file = state_dir / "empty-secrets.toml"
    if secrets_file.is_symlink() or not secrets_file.is_file() or secrets_file.stat().st_size != 0:
        raise RuntimeError("Only the launcher-owned empty Streamlit secrets file is permitted")


def disable_address_discovery() -> None:
    # Streamlit's default origin checker can contact an external IP-discovery
    # service for rejected origins. This local-only app must not do so. Keep
    # strict allowedHosts/CORS/XSRF enabled and remove only address discovery.
    from streamlit import net_util

    if not callable(getattr(net_util, "get_internal_ip", None)) or not callable(
        getattr(net_util, "get_external_ip", None)
    ):
        raise RuntimeError("Pinned Streamlit address-discovery guard is incompatible; refusing startup")
    net_util.get_internal_ip = lambda: None
    net_util.get_external_ip = lambda: None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal isolated Streamlit bootstrap")
    parser.add_argument("port", type=int)
    args = parser.parse_args(argv)
    raw = os.environ.get("REN_MONITOR_STATE_DIR")
    if not raw:
        raise RuntimeError("Use the ren-monitor launcher; an isolated runtime directory is required")
    state_dir = Path(raw)
    validate_isolation(state_dir)
    disable_address_discovery()
    from streamlit.web.cli import main as streamlit_main

    streamlit_main(args=streamlit_flags(args.port, state_dir), prog_name="streamlit", standalone_mode=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
