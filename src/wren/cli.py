"""Wren command-line launcher and desktop server runner."""
import argparse
import os
import threading
import time

import uvicorn

from wren.core.controller import WrenController
from wren.core.state import WrenState
from wren.ui.orb_preview import create_app, get_network_ips, resolve_auth_token

HOTKEY_ENV = "WREN_HOTKEY"
DEFAULT_HOTKEY = "<ctrl>+<alt>+<space>"
QUIT_HOTKEY_ENV = "WREN_QUIT_HOTKEY"
DEFAULT_QUIT_HOTKEY = "<ctrl>+<alt>+q"
MIN_HOLD_SECONDS = 0.4
MAX_LISTEN_SECONDS = 120.0


def _parse_hotkey(spec: str) -> set:
    """Parse '<ctrl>+<alt>+<space>' into a set of pynput key objects."""
    from pynput.keyboard import Key

    named = {
        "ctrl": Key.ctrl,
        "control": Key.ctrl,
        "alt": Key.alt,
        "shift": Key.shift,
        "super": Key.cmd,
        "cmd": Key.cmd,
        "win": Key.cmd,
        "space": Key.space,
        "enter": Key.enter,
        "esc": Key.esc,
        "tab": Key.tab,
    }
    keys = set()
    for part in spec.strip().lower().strip("<>").split("+"):
        part = part.strip().strip("<>")
        if not part:
            continue
        if part in named:
            keys.add(named[part])
        elif len(part) == 1:
            keys.add(part)
        else:
            raise ValueError(f"Unsupported hotkey key '{part}' in {spec!r}")
    return keys


def _start_hotkey_listener(controller: WrenController) -> None:
    """Global hold-to-talk hotkey: press and hold to dictate, release to process.

    Runs on a daemon thread; silently disabled when pynput is unavailable.
    Override the combo with WREN_HOTKEY (e.g. '<ctrl>+<alt>+<space>').
    """
    try:
        from pynput import keyboard
    except ImportError:
        print("pynput not installed — global hotkey disabled")
        return

    try:
        combo = _parse_hotkey(os.environ.get(HOTKEY_ENV, DEFAULT_HOTKEY))
    except Exception as exc:
        print(f"Bad {HOTKEY_ENV}: {exc} — using {DEFAULT_HOTKEY}")
        combo = _parse_hotkey(DEFAULT_HOTKEY)

    quit_spec = os.environ.get(QUIT_HOTKEY_ENV, DEFAULT_QUIT_HOTKEY).strip()
    quit_combo = None
    if quit_spec:
        try:
            quit_combo = _parse_hotkey(quit_spec)
        except Exception as exc:
            print(f"Bad {QUIT_HOTKEY_ENV}: {exc} — quit hotkey disabled")

    def _quit() -> None:
        def run():
            try:
                import subprocess

                subprocess.run(
                    ["systemctl", "--user", "stop", "wren.service"],
                    timeout=10,
                    capture_output=True,
                )
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=run, daemon=True).start()

    def _canon(key):
        # pynput delivers printable keys as KeyCode objects whose .char is the
        # character; KeyCode('q') != 'q', so normalize to the plain char.
        char = getattr(key, "char", None)
        return char if isinstance(char, str) and len(char) == 1 else key

    pressed: set = set()
    active = threading.Event()
    start_done = threading.Event()
    started_at = [0.0]
    quit_armed = threading.Event()

    def _combo_down(keys) -> bool:
        return keys.issubset(pressed) and len(pressed) == len(keys)

    def _start() -> None:
        if active.is_set():
            return
        if controller.state in (WrenState.IDLE, WrenState.DRAFT_READY, WrenState.CLIPBOARD_WRITTEN):
            start_done.clear()

            def run():
                try:
                    controller.start_listening()
                finally:
                    start_done.set()

            threading.Thread(target=run, daemon=True).start()
            active.set()
            started_at[0] = time.monotonic()
            threading.Thread(target=_failsafe, daemon=True).start()

    def _stop() -> None:
        if not active.is_set():
            return
        active.clear()
        # Wait out an in-flight start so a quick tap can't strand LISTENING.
        start_done.wait(timeout=3.0)
        held = time.monotonic() - started_at[0]
        if controller.state != WrenState.LISTENING:
            return
        if held < MIN_HOLD_SECONDS:
            controller.cancel()
            return
        threading.Thread(target=controller.stop_listening, daemon=True).start()

    def _failsafe() -> None:
        time.sleep(MAX_LISTEN_SECONDS)
        if active.is_set() and controller.state == WrenState.LISTENING:
            _stop()

    def on_press(key):
        pressed.add(_canon(key))
        if quit_combo is not None and _combo_down(quit_combo):
            quit_armed.set()
        if _combo_down(combo):
            _start()

    def on_release(key):
        pressed.discard(_canon(key))
        if quit_armed.is_set():
            # First key-up after the quit combo was armed ends the combo → quit.
            quit_armed.clear()
            _quit()
            return
        if not _combo_down(combo):
            _stop()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    key_names = "+".join(sorted(str(k) for k in combo))
    print(f"Global dictation hotkey: hold {key_names} (override with {HOTKEY_ENV})")
    if quit_combo is not None:
        quit_names = "+".join(sorted(str(k) for k in quit_combo))
        print(f"Global quit hotkey: tap {quit_names} (override with {QUIT_HOTKEY_ENV})")


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
        _start_hotkey_listener(controller)

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
