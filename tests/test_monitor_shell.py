"""Native shell contract tests are not claims of a real native-window launch."""

from __future__ import annotations

import socket
import sys
from types import SimpleNamespace

import pytest

from workbench_pilot import monitor_launcher as launcher
from workbench_pilot.monitor_bootstrap import streamlit_flags

pytestmark = pytest.mark.monitor


def test_native_headless_guard_starts_no_children(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    def must_not_construct(*args, **kwargs):
        raise AssertionError("No runtime should be created before headless validation")

    monkeypatch.setattr(launcher, "MonitorRuntime", must_not_construct)
    assert launcher.main(["--data-dir", str(tmp_path / "unused")]) == 2
    assert "No graphical display" in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()


def test_native_shell_contract_uses_local_url_and_finally_cleanup(monkeypatch):
    calls = []

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            calls.append("constructed")

        def start(self):
            return "http://127.0.0.1:8501"

        def close(self):
            calls.append("closed")

    def create_window(title, url, **kwargs):
        calls.append((title, url, kwargs))
        assert "js_api" not in kwargs

    def start(**kwargs):
        assert kwargs == {"debug": False, "private_mode": True}
        calls.append("gui-contract-only")

    monkeypatch.setattr(launcher, "check_native_window", lambda: None)
    monkeypatch.setattr(launcher, "MonitorRuntime", FakeRuntime)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start))
    assert launcher.main([]) == 0
    assert calls[-1] == "closed"
    assert calls[1][1] == "http://127.0.0.1:8501"


def test_failed_native_backend_still_closes_owned_runtime(monkeypatch, capsys):
    closed = []

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return "http://127.0.0.1:8501"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(launcher, "check_native_window", lambda: None)
    monkeypatch.setattr(launcher, "MonitorRuntime", FakeRuntime)

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic missing GUI backend")

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=fail))
    assert launcher.main([]) == 2
    assert closed == [True]
    assert "Synthetic missing GUI backend" in capsys.readouterr().err


def test_commands_are_fixed_argv_and_loopback_only(tmp_path):
    command = launcher.ren_command(8765, tmp_path)
    assert command[:4] == (sys.executable, "-u", "-m", "workbench_pilot")
    assert "--console-events" in command
    command = launcher.streamlit_command(8501)
    assert command[2:4] == ("-m", "workbench_pilot.monitor_bootstrap")
    assert command[-1] == "8501"
    dashboard = streamlit_flags(8501, tmp_path)
    assert "--server.address=127.0.0.1" in dashboard
    assert "--browser.gatherUsageStats=false" in dashboard
    assert "--server.enableCORS=true" in dashboard
    assert "--server.enableXsrfProtection=true" in dashboard
    assert "--server.fileWatcherType=none" in dashboard
    for bad in [True, 0, -1, 65536, "8501;echo bad"]:
        with pytest.raises(ValueError):
            launcher.streamlit_command(bad)
    with pytest.raises(ValueError):
        launcher.MonitorRuntime(8765, 8765, tmp_path)


def test_busy_port_never_attaches_or_stops_existing_process(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        runtime = launcher.MonitorRuntime(port, port + 1 if port < 65535 else port - 1, tmp_path)
        with pytest.raises(RuntimeError, match="No existing process"):
            runtime.start()
        assert runtime.ren is None
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        runtime.close()


def test_cleanup_failure_produces_nonzero_cli_exit(monkeypatch, capsys):
    class FailedCleanupRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return "http://127.0.0.1:8501"

        def close(self):
            raise RuntimeError("Synthetic unconfirmed owned cleanup")

    monkeypatch.setattr(launcher, "check_native_window", lambda: None)
    monkeypatch.setattr(launcher, "MonitorRuntime", FailedCleanupRuntime)
    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(create_window=lambda *args, **kwargs: None, start=lambda **kwargs: None),
    )
    assert launcher.main([]) == 2
    assert "unconfirmed owned cleanup" in capsys.readouterr().err
