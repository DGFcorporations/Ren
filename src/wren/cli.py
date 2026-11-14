"""Wren command-line launcher, global hotkeys, and floating plasma orb runner."""
import argparse
import os
import subprocess
import threading
import time

import uvicorn

from wren.core.controller import WrenController
from wren.core.state import WrenState
from wren.ui.native_orb import NativeOrbWindow
from wren.ui.orb_preview import create_app, get_network_ips, resolve_auth_token

HOTKEY_ENV = "WREN_HOTKEY"
DEFAULT_HOTKEY = "<ctrl>+<shift>"
TALK_HOTKEY_ENV = "WREN_TALK_HOTKEY"
DEFAULT_TALK_HOTKEY = "<alt>+<space>"
QUIT_HOTKEY_ENV = "WREN_QUIT_HOTKEY"
DEFAULT_QUIT_HOTKEY = "<ctrl>+<alt>+q"
MODIFIER_ARM_DELAY = 0.16
MIN_HOLD_SECONDS = 0.35
MAX_LISTEN_SECONDS = 120.0


def _canon_key(key):
    """Normalize left/right modifier variants and KeyCode characters for reliable set matching."""
    from pynput.keyboard import Key

    lr_map = {
        Key.ctrl_l: Key.ctrl,
        Key.ctrl_r: Key.ctrl,
        Key.shift_l: Key.shift,
        Key.shift_r: Key.shift,
        Key.alt_l: Key.alt,
        Key.alt_r: Key.alt,
        Key.alt_gr: Key.alt,
        Key.cmd_l: Key.cmd,
        Key.cmd_r: Key.cmd,
    }
    if key in lr_map:
        return lr_map[key]

    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1:
        code = ord(char)
        if 1 <= code <= 26:
            return chr(code + 96)
        return char.lower()
    return key


def _is_modifier_only(combo: set) -> bool:
    from pynput.keyboard import Key

    mods = {Key.ctrl, Key.shift, Key.alt, Key.cmd}
    return bool(combo) and combo.issubset(mods)


def _parse_hotkey(spec: str) -> set:
    """Parse '<ctrl>+<shift>' or '<alt>+<space>' into canonical pynput key objects."""
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
    """Global hold-to-talk / hold-to-dictate / tap-to-quit hotkeys via pynput.

    Defaults:
      - Hold Ctrl+Shift -> Dictate to preview pill (leaves OpenWhispr's Ctrl+Alt untouched)
      - Hold Alt+Space  -> Two-way voice conversation with Ren (speaks back via Qwen/Deepgram)
      - Tap  Ctrl+Alt+Q -> Quit Ren cleanly

    Modifier-only combos (Ctrl+Shift) include chord-taint protection: pressing any
    additional key (e.g. Ctrl+Shift+C / Ctrl+Shift+V) immediately aborts dictation.
    """
    try:
        from pynput import keyboard
    except ImportError:
        print("pynput not installed — global hotkeys disabled")
        return

    def _safe_combo(env_name: str, default_spec: str) -> set | None:
        raw = os.environ.get(env_name, default_spec).strip()
        if not raw:
            return None
        try:
            parsed = _parse_hotkey(raw)
            return parsed or None
        except Exception as exc:
            print(f"Bad {env_name}: {exc} — using {default_spec}")
            return _parse_hotkey(default_spec)

    dictate_combo = _safe_combo(HOTKEY_ENV, DEFAULT_HOTKEY)
    talk_combo = _safe_combo(TALK_HOTKEY_ENV, DEFAULT_TALK_HOTKEY)
    quit_combo = _safe_combo(QUIT_HOTKEY_ENV, DEFAULT_QUIT_HOTKEY)

    def _quit() -> None:
        def run():
            try:
                subprocess.run(
                    ["systemctl", "--user", "stop", "wren.service"],
                    timeout=10,
                    capture_output=True,
                )
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=run, daemon=True).start()

    pressed: set = set()
    active_combo: list[set | None] = [None]
    active = threading.Event()
    arming_id = [0]
    chord_tainted = threading.Event()
    start_done = threading.Event()
    started_at = [0.0]
    quit_armed = threading.Event()

    ready_states = (WrenState.IDLE, WrenState.DRAFT_READY, WrenState.CLIPBOARD_WRITTEN, WrenState.ERROR)
    busy_paste_states = (
        WrenState.PASTING,
        WrenState.TARGET_VERIFY,
        WrenState.PASTE_PENDING_APPROVAL,
    )

    def _exact_down(keys: set | None) -> bool:
        return keys is not None and pressed == keys

    def _begin_capture(combo: set, intent: str, token: int) -> None:
        if arming_id[0] != token or chord_tainted.is_set() or active.is_set():
            return
        if pressed != combo or controller.state in busy_paste_states:
            return
        if controller.state not in ready_states:
            return

        active_combo[0] = combo
        start_done.clear()
        active.set()
        started_at[0] = time.monotonic()

        def run():
            try:
                controller.start_listening(intent=intent)
            finally:
                start_done.set()

        threading.Thread(target=run, daemon=True).start()
        threading.Thread(target=_failsafe, args=(token,), daemon=True).start()

    def _schedule_start(combo: set, intent: str) -> None:
        if active.is_set() or controller.state in busy_paste_states:
            return
        chord_tainted.clear()
        arming_id[0] += 1
        token = arming_id[0]
        if _is_modifier_only(combo):
            t = threading.Timer(MODIFIER_ARM_DELAY, _begin_capture, args=(combo, intent, token))
            t.daemon = True
            t.start()
        else:
            _begin_capture(combo, intent, token)

    def _abort_tainted() -> None:
        chord_tainted.set()
        arming_id[0] += 1
        if active.is_set():
            active.clear()
            active_combo[0] = None
            start_done.wait(timeout=1.5)
            controller.cancel()

    def _stop() -> None:
        if not active.is_set():
            arming_id[0] += 1
            return
        active.clear()
        active_combo[0] = None
        start_done.wait(timeout=3.0)
        if chord_tainted.is_set():
            controller.cancel()
            return
        held = time.monotonic() - started_at[0]
        if controller.state != WrenState.LISTENING:
            return
        if held < MIN_HOLD_SECONDS:
            controller.cancel()
            return
        threading.Thread(target=controller.stop_listening, daemon=True).start()

    def _failsafe(token: int) -> None:
        time.sleep(MAX_LISTEN_SECONDS)
        if arming_id[0] == token and active.is_set() and controller.state == WrenState.LISTENING:
            _stop()

    def on_press(key):
        ck = _canon_key(key)
        if ck in pressed:
            return
        pressed.add(ck)

        if quit_combo is not None and _exact_down(quit_combo):
            _abort_tainted()
            quit_armed.set()
            return

        # If a modifier-only hold is armed/active and the user presses an extra key
        # (e.g. Ctrl+Shift+C or Ctrl+Shift+V), taint and cancel immediately.
        cur = active_combo[0]
        if cur is not None and not pressed.issubset(cur):
            _abort_tainted()
            return
        if not active.is_set() and dictate_combo is not None and len(pressed) > len(dictate_combo):
            if dictate_combo.issubset(pressed):
                chord_tainted.set()
                arming_id[0] += 1

        if _exact_down(talk_combo):
            _schedule_start(talk_combo, "talk")
        elif _exact_down(dictate_combo):
            _schedule_start(dictate_combo, "dictate")

    def on_release(key):
        ck = _canon_key(key)
        pressed.discard(ck)
        if quit_armed.is_set():
            quit_armed.clear()
            _quit()
            return
        cur = active_combo[0]
        if cur is not None and not cur.issubset(pressed):
            _stop()
        elif not pressed:
            chord_tainted.clear()
            arming_id[0] += 1

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    if dictate_combo:
        d_names = "+".join(sorted(str(k) for k in dictate_combo))
        print(f"Dictate hotkey: hold {d_names} (override with {HOTKEY_ENV})")
    if talk_combo:
        t_names = "+".join(sorted(str(k) for k in talk_combo))
        print(f"Talk hotkey: hold {t_names} (override with {TALK_HOTKEY_ENV})")
    if quit_combo:
        q_names = "+".join(sorted(str(k) for k in quit_combo))
        print(f"Quit hotkey: tap {q_names} (override with {QUIT_HOTKEY_ENV})")


def main():
    parser = argparse.ArgumentParser(description="Wren: Local Voice & Text Assistant")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for non-loopback clients "
        "(default: WREN_AUTH_TOKEN, then ~/.config/wren/auth-token)",
    )
    parser.add_argument("--model", default="auto", help="LLM router model (default: auto)")
    parser.add_argument("--stt", default="hybrid", help="STT provider (default: hybrid)")
    parser.add_argument(
        "--native",
        action="store_true",
        help="Launch as native frameless RGBA floating plasma orb",
    )
    args = parser.parse_args()
    token, retired = resolve_auth_token(args.token, persist=True)
    ips = get_network_ips()

    print(f"=== Starting Ren Plasma Orb on port {args.port} ===")
    print(f"Local orb: http://127.0.0.1:{args.port}")
    if ips["lan_ip"]:
        print(f"LAN companion: http://{ips['lan_ip']}:{args.port}")
    if ips["tailscale_ip"]:
        print(f"Tailscale companion: http://{ips['tailscale_ip']}:{args.port}")
    if retired:
        print("The old built-in token was ignored.")
    print("Pairing token (type this into the phone; it is not in the source):")
    print(token)
    print(f"STT: {args.stt} | TTS: {os.environ.get('WREN_TTS_PROVIDER', 'qwen')} | LLM: {args.model}")

    controller = WrenController(stt_provider=args.stt, model=args.model)

    if args.native:
        connect_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        orb_url = f"http://{connect_host}:{args.port}/?desktop=1"
        orb_win = NativeOrbWindow(orb_url)
        app = create_app(controller, auth_token=token, window_hooks=orb_win)

        server_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(server_config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(0.8)
        _start_hotkey_listener(controller)

        print(f"Launching frameless RGBA floating plasma orb at {orb_url}...")
        if not orb_win.run():
            import webview

            webview.create_window(
                "Wren Assistant",
                orb_url,
                width=340,
                height=390,
                resizable=False,
                frameless=True,
                on_top=True,
            )
            webview.start()
    else:
        app = create_app(controller, auth_token=token)
        print("Press Ctrl+C to stop.")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
