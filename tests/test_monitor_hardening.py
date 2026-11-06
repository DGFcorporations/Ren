"""Regression cases from the monitor's independent cross-component review."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from workbench_pilot.monitor_bootstrap import streamlit_flags, validate_isolation
from workbench_pilot.monitor_bridge import MonitorBridge
from workbench_pilot.monitor_launcher import MonitorRuntime
from workbench_pilot.process_monitor import OwnedProcessMonitor, safe_child_env

pytestmark = pytest.mark.monitor


def test_framework_configuration_is_explicitly_local_and_empty_secrets(tmp_path):
    flags = streamlit_flags(8501, tmp_path)
    assert "--server.allowedHosts=127.0.0.1" in flags
    assert "--server.corsAllowedOrigins=http://127.0.0.1:8501" in flags
    assert "--server.enableCORS=true" in flags
    assert "--server.enableXsrfProtection=true" in flags
    assert f"--secrets.files={tmp_path / 'empty-secrets.toml'}" in flags
    assert "--browser.gatherUsageStats=false" in flags


def test_isolation_refuses_ambient_cwd_or_nonempty_secrets(tmp_path, monkeypatch):
    state = tmp_path / "isolated"
    (state / "home").mkdir(parents=True)
    (state / "work").mkdir()
    secrets_file = state / "empty-secrets.toml"
    secrets_file.write_text("")
    monkeypatch.setenv("HOME", str(state / "home"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="not isolated"):
        validate_isolation(state)
    monkeypatch.chdir(state / "work")
    validate_isolation(state)
    secrets_file.write_text('OPENAI_API_KEY="SYNTHETIC-NOT-A-CREDENTIAL"')
    with pytest.raises(RuntimeError, match="empty Streamlit secrets"):
        validate_isolation(state)


def test_cookie_setup_and_bad_origin_do_not_load_ambient_keys_or_discover_ips(tmp_path):
    state = tmp_path / "isolated"
    (state / "home" / ".streamlit").mkdir(parents=True)
    (state / "work" / ".streamlit").mkdir(parents=True)
    (state / "empty-secrets.toml").write_text("")
    # Deliberately poisoned but entirely synthetic config files. No real secrets
    # are read: the probe runs with a generated, tightly scoped environment.
    for directory in [state / "home", state / "work"]:
        (directory / ".streamlit" / "secrets.toml").write_text('OPENAI_API_KEY="SYNTHETIC-SHOULD-NOT-LOAD"\n')
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env = safe_child_env({"PYTHONPATH": source_root, "REN_MONITOR_STATE_DIR": str(state)})
    env["HOME"] = str(state / "home")
    script = """
import json,os
from pathlib import Path
from workbench_pilot.monitor_bootstrap import validate_isolation,disable_address_discovery
state=Path(os.environ['REN_MONITOR_STATE_DIR'])
validate_isolation(state)
disable_address_discovery()
from streamlit import config,net_util
config.set_option('secrets.files',[str(state/'empty-secrets.toml')])
config.set_option('server.enableCORS',True)
config.set_option('browser.serverAddress','127.0.0.1')
from streamlit.web.server.server_util import get_cookie_secret,is_url_from_allowed_origins
get_cookie_secret()
print(json.dumps({'key_loaded':'OPENAI_API_KEY' in os.environ,
                 'external_ip':net_util.get_external_ip(),
                 'internal_ip':net_util.get_internal_ip(),
                 'bad_origin_allowed':is_url_from_allowed_origins('https://foreign.invalid')}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=state / "work",
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert data == {
        "key_loaded": False,
        "external_ip": None,
        "internal_ip": None,
        "bad_origin_allowed": False,
    }
    assert "SYNTHETIC-SHOULD-NOT-LOAD" not in result.stdout + result.stderr


def test_bridge_thread_start_failure_is_cleanup_safe(monkeypatch):
    process = OwnedProcessMonitor((sys.executable, "-c", "print('unused')"))
    bridge = MonitorBridge(process, "http://127.0.0.1:8765")
    original = threading.Thread.start

    def fail_only_bridge(thread):
        if thread.name == "ren-monitor-bridge":
            raise RuntimeError("synthetic thread-start failure")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_only_bridge)
    try:
        with pytest.raises(RuntimeError, match="thread-start failure"):
            bridge.start()
        before = time.monotonic()
        bridge.close()
        assert time.monotonic() - before < 0.5
        assert bridge.server is None and bridge.thread is None
    finally:
        process.close()


def test_runtime_propagates_unconfirmed_cleanup_without_skipping_other_resources(tmp_path, monkeypatch):
    runtime = MonitorRuntime(8765, 8501, tmp_path)
    child = OwnedProcessMonitor((sys.executable, "-c", "print('not started')"))
    other_closed = []

    class Bridge:
        def close(self):
            other_closed.append(True)

    runtime.dashboard = child
    runtime.bridge = Bridge()
    original_snapshot = child.snapshot

    def failed_snapshot():
        return {**original_snapshot(), "state": "failed", "error": "synthetic unconfirmed cleanup"}

    monkeypatch.setattr(child, "snapshot", failed_snapshot)
    with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
        runtime.close()
    assert other_closed == [True]
    with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
        runtime.close()
    assert original_snapshot()["state"] == "closed"  # No actual child was left running by this test.
