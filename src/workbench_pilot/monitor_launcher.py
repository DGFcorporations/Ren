"""Own Ren + Streamlit and optionally show the monitor in a native webview shell."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .monitor_bridge import MonitorBridge, NoRedirect
from .process_monitor import OwnedProcessMonitor, safe_child_env


class NativeWindowUnavailable(RuntimeError):
    pass


def check_native_window() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise NativeWindowUnavailable("The native webview must run on the main thread")
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        raise NativeWindowUnavailable(
            "No graphical display is available. Native pywebview launch is unverified here. "
            "Use the explicit --web mode for a browser view; it is not a native-window test."
        )
    if importlib.util.find_spec("webview") is None:
        raise NativeWindowUnavailable(
            "pywebview is not installed; install the desktop extra on a supported host"
        )


def _port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Ports must be integers between 1 and 65535")
    return port


def _require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"Loopback port {port} is unavailable. No existing process will be attached to or stopped."
            ) from exc


def ren_command(ren_port: int, data_dir: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-u",
        "-m",
        "workbench_pilot",
        "serve",
        "--port",
        str(_port(ren_port)),
        "--data-dir",
        str(data_dir.resolve()),
        "--console-events",
    )


def streamlit_command(monitor_port: int) -> tuple[str, ...]:
    return (sys.executable, "-u", "-m", "workbench_pilot.monitor_bootstrap", str(_port(monitor_port)))


class MonitorRuntime:
    """Launcher owns both child handles; Streamlit reruns only view/control the bridge."""

    def __init__(self, ren_port: int, monitor_port: int, data_dir: Path, *, autostart=True):
        self.ren_port, self.monitor_port = _port(ren_port), _port(monitor_port)
        if self.ren_port == self.monitor_port:
            raise ValueError("Ren and Streamlit must use different ports")
        if os.name != "posix":
            raise RuntimeError("Owned-process cleanup is qualified for POSIX only; Windows is unverified")
        if type(autostart) is not bool:
            raise ValueError("autostart must be a boolean")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.autostart = autostart
        self.url = f"http://127.0.0.1:{self.monitor_port}"
        self.ren = self.bridge = self.dashboard = None
        self._closed = False
        self._runtime_dir = None
        self._cleanup_error = None

    def start(self, *, timeout=30.0) -> str:
        if self._closed or self.ren is not None:
            raise RuntimeError("A launcher runtime is single-use")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("Startup timeout must be finite, positive and at most 60 seconds")
        _require_free_port(self.ren_port)
        _require_free_port(self.monitor_port)
        source_root = str(Path(__file__).resolve().parents[1])
        try:
            self.ren = OwnedProcessMonitor(
                ren_command(self.ren_port, self.data_dir),
                env=safe_child_env({"PYTHONPATH": source_root}),
            )
            self.bridge = MonitorBridge(self.ren, f"http://127.0.0.1:{self.ren_port}")
            bridge_origin = self.bridge.start()
            self._runtime_dir = tempfile.TemporaryDirectory(prefix="ren-monitor-isolated-")
            state_dir = Path(self._runtime_dir.name).resolve()
            (state_dir / "home").mkdir(mode=0o700)
            (state_dir / "work").mkdir(mode=0o700)
            (state_dir / "empty-secrets.toml").write_text("")
            env = safe_child_env(
                {
                    "PYTHONPATH": source_root,
                    "REN_MONITOR_CONTROL_URL": bridge_origin,
                    "REN_MONITOR_CONTROL_TOKEN": self.bridge.token,
                    "REN_MONITOR_STATE_DIR": str(state_dir),
                }
            )
            env["HOME"] = str(state_dir / "home")
            self.dashboard = OwnedProcessMonitor(
                streamlit_command(self.monitor_port), env=env, cwd=state_dir / "work"
            )
            self.dashboard.start().result(timeout=5)
            if self.autostart:
                self.ren.start().result(timeout=5)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = self.dashboard.snapshot()
                if state["state"] in {"failed", "exited", "closed"}:
                    raise RuntimeError("Streamlit exited before becoming ready: " + state["text"][-2000:])
                try:
                    with opener.open(self.url, timeout=0.5) as response:
                        if response.status == 200:
                            return self.url
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(0.05)
            raise RuntimeError("Timed out waiting for the owned Streamlit server")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            if self._cleanup_error:
                raise RuntimeError(self._cleanup_error)
            return
        self._closed = True
        errors = []
        for label, resource in (("Streamlit", self.dashboard), ("bridge", self.bridge), ("Ren", self.ren)):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    errors.append(label + ": " + type(exc).__name__)
                if isinstance(resource, OwnedProcessMonitor):
                    try:
                        if resource.snapshot()["state"] != "closed":
                            errors.append(label + ": process cleanup is unconfirmed")
                    except Exception:
                        errors.append(label + ": cleanup state is unavailable")
        if self._runtime_dir is not None and not errors:
            try:
                self._runtime_dir.cleanup()
                self._runtime_dir = None
            except OSError:
                errors.append("isolated runtime directory cleanup failed")
        if errors:
            self._cleanup_error = "Owned runtime cleanup could not be fully confirmed: " + ", ".join(errors)
            raise RuntimeError(self._cleanup_error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ren-monitor", description="Local G1 stdout/stderr monitor")
    parser.add_argument(
        "--web", action="store_true", help="Browser mode, explicitly separate from native webview"
    )
    parser.add_argument("--ren-port", type=int, default=8765)
    parser.add_argument("--monitor-port", type=int, default=8501)
    parser.add_argument("--data-dir", type=Path, default=Path(".pilot-data"))
    parser.add_argument("--no-autostart", action="store_true", help="Wait for the Start Ren button")
    args = parser.parse_args(argv)
    runtime = None
    previous_handlers = {}
    stopped = threading.Event()
    closing = threading.Event()
    exit_code = 0

    def request_stop(_signum, _frame):
        stopped.set()
        if not args.web and not closing.is_set():
            raise KeyboardInterrupt

    try:
        if not args.web:
            check_native_window()  # Fail before starting any children in a headless runtime.
        runtime = MonitorRuntime(
            args.ren_port, args.monitor_port, args.data_dir, autostart=not args.no_autostart
        )
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, request_stop)
        url = runtime.start()
        print(f"Ren output monitor: {url}", flush=True)
        print("Local G1 only. No stdin relay, microphone, WebRTC or live providers enabled.", flush=True)
        if args.web:
            while not stopped.wait(0.25):
                state = runtime.dashboard.snapshot()
                if state["state"] in {"failed", "exited", "closed"}:
                    raise RuntimeError("The owned Streamlit process exited; closing the monitor")
        else:
            import webview

            # No js_api, filesystem server, profile copy or external URL is exposed.
            webview.create_window("Ren · G1 output monitor", url, width=1100, height=800)
            webview.start(debug=False, private_mode=True)
    except KeyboardInterrupt:
        exit_code = 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 2
    finally:
        closing.set()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:
                print(str(exc), file=sys.stderr)
                exit_code = 2
        for sig, handler in previous_handlers.items():
            with contextlib.suppress(ValueError):
                signal.signal(sig, handler)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
