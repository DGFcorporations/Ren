"""Wren command-line launcher and desktop server runner."""
import argparse
import threading
import time

import uvicorn

from wren.core.controller import WrenController
from wren.ui.orb_preview import create_app, get_network_ips, resolve_auth_token


def main():
    parser = argparse.ArgumentParser(description="Wren: Local Voice & Text Assistant")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for clients that are not on this computer (default: WREN_AUTH_TOKEN, then ~/.config/wren/auth-token)",
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
    token, retired = resolve_auth_token(args.token, persist=True)
    ips = get_network_ips()

    print(f"=== Starting Wren Desktop Assistant on port {args.port} ===")
    print(f"Local widget: http://127.0.0.1:{args.port}")
    if ips["lan_ip"]:
        print(f"LAN companion: http://{ips['lan_ip']}:{args.port}")
    else:
        print("LAN companion: no LAN address detected")
    if ips["tailscale_ip"]:
        print(f"Tailscale companion: http://{ips['tailscale_ip']}:{args.port}")
    else:
        print("Tailscale companion: not detected")
    if retired:
        print("The old built-in token was ignored.")
    print("Pairing token (type this into the phone; it is not in the source):")
    print(token)
    print("Auth: bearer token required for every client that is not on this computer")
    print(f"STT: {args.stt} | LLM Model: {args.model}")

    controller = WrenController(stt_provider=args.stt, model=args.model)
    app = create_app(controller, auth_token=token)

    if args.native:
        import webview

        # Run uvicorn in a daemon thread
        server_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(server_config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.0)

        connect_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
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
