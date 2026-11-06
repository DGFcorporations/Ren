"""Offline-only CLI. G1 has no provider flags, API keys, or live mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapters import integration_status
from .policy import PilotSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workbench-pilot", description="G1 synthetic offline pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Print the explicit offline/unverified capability status")
    serve = sub.add_parser("serve", help="Run the local-only fixture UI; no providers or microphone")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--data-dir", type=Path, default=Path(".pilot-data"))
    serve.add_argument(
        "--no-fault-controls",
        action="store_true",
        help="Disable the controlled synthetic fault-injection UI/API",
    )
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(integration_status(), indent=2))
        return 0
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn

    from .web import create_app

    settings = PilotSettings(
        args.data_dir, f"http://127.0.0.1:{args.port}", allow_fault_injection=not args.no_fault_controls
    )
    print("G1 OFFLINE: synthetic transcripts + test tones, not real speech recognition/synthesis.")
    print("No live provider integration or real-account browser access is enabled.")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")
    return 0
