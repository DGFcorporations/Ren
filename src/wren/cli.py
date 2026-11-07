"""Wren command-line launcher and desktop server runner."""
import argparse

import uvicorn

from wren.core.controller import WrenController
from wren.ui.orb_preview import create_app


def main():
    parser = argparse.ArgumentParser(description="Wren: Local Voice & Text Assistant")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--model", default="auto", help="FreeLLMAPI model (default: auto)")
    parser.add_argument(
        "--stt",
        default="faster-whisper",
        choices=["faster-whisper", "mock"],
        help="STT engine",
    )
    args = parser.parse_args()

    print(f"=== Starting Wren Desktop Assistant on http://{args.host}:{args.port} ===")
    print(f"STT: {args.stt} | LLM Model: {args.model}")
    print("Press Ctrl+C to stop.")

    controller = WrenController(stt_provider=args.stt, model=args.model)
    app = create_app(controller)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
