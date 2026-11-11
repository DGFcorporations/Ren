"""Wren command-line launcher and desktop server runner."""
import argparse
import os
import threading
import time

import uvicorn

from wren.core.controller import WrenController
from wren.ui.orb_preview import create_app


def main():
    parser = argparse.ArgumentParser(description="Wren: Local Voice & Text Assistant")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface (default: 127.0.0.1; remote hosting is not enabled)",
    )
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument(
        "--token",
        default=os.environ.get("WREN_AUTH_TOKEN", ""),
        help="Optional bearer token; no built-in remote credential is provided",
    )
    parser.add_argument("--model", default="auto", help="FreeLLMAPI model (default: auto)")
    parser.add_argument(
        "--stt",
        default="faster-whisper",
        choices=["faster-whisper", "mock"],
        help="STT engine",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="Launch as native floating desktop window",
    )
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error(
            "Direct non-loopback hosting is disabled until secure phone pairing and TLS are implemented. "
            "Keep Wren on loopback and use an authorized private TLS proxy when one is configured."
        )

    print(f"=== Starting Wren Desktop Assistant on port {args.port} ===")
    print(f"Local Widget: http://127.0.0.1:{args.port}")
    print("Remote companion: not configured; server is local-only by default.")
    print(f"Auth: {'Bearer token configured' if args.token else 'No remote token configured'}")
    print(f"STT: {args.stt} | LLM Model: {args.model}")

    controller = WrenController(stt_provider=args.stt, model=args.model)
    app = create_app(controller, auth_token=args.token)

    if args.native:
        import webview

        # Run uvicorn in a daemon thread
        server_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(server_config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.0)

        connect_host = args.host
        print(f"Opening native floating window (always-on-top) at http://{connect_host}:{args.port}...")
        webview.create_window(
            "Wren Assistant",
            f"http://{connect_host}:{args.port}",
            width=400,
            height=640,
            resizable=True,
            on_top=True,
        )
        webview.start()
    else:
        print("Press Ctrl+C to stop.")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
