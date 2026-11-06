"""Authenticated local monitor commands; no arbitrary commands, stdin or external URLs."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager

import httpx
import pytest

from workbench_pilot.monitor_bridge import BridgeClient, MonitorBridge, checked_origin
from workbench_pilot.process_monitor import OwnedProcessMonitor

pytestmark = pytest.mark.monitor


@contextmanager
def bridge_fixture():
    process = OwnedProcessMonitor(
        (sys.executable, "-u", "-c", "import time;print('BRIDGE_READY');time.sleep(30)"), stop_timeout=0.3
    )
    bridge = MonitorBridge(process, "http://127.0.0.1:8765")
    bridge.start()
    try:
        yield process, bridge, BridgeClient(bridge.origin, bridge.token)
    finally:
        bridge.close()
        process.close()


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("Local test condition did not occur")


def test_fixed_control_commands_capture_owned_output():
    with bridge_fixture() as (process, bridge, client):
        assert client.snapshot()["state"] == "idle"
        assert client.command("start")["accepted"]
        until(lambda: "BRIDGE_READY" in client.snapshot()["text"])
        pid = client.snapshot()["pid"]
        client.command("start")
        assert client.snapshot()["pid"] == pid
        assert client.snapshot()["generation"] == 1
        assert client.command("stop")["accepted"]
        until(lambda: client.snapshot()["state"] == "exited")
        client.command("clear")
        assert client.snapshot()["text"] == ""
        assert client.snapshot()["live_services"] == "disabled and unverified"
        with pytest.raises(ValueError):
            client.command("send_prompt")
        with pytest.raises(ValueError):
            client.command("kill/1234")


def test_authentication_origin_host_and_arguments():
    with bridge_fixture() as (process, bridge, client), httpx.Client(trust_env=False) as http:
        assert http.get(bridge.origin + "/snapshot").status_code == 403
        headers = {"X-Ren-Monitor-Token": bridge.token}
        assert (
            http.get(bridge.origin + "/snapshot", headers={**headers, "Origin": bridge.origin}).status_code
            == 403
        )
        assert (
            http.get(bridge.origin + "/snapshot", headers={**headers, "Host": "foreign.invalid"}).status_code
            == 400
        )
        assert http.get(bridge.origin + "/snapshot?command=bad", headers=headers).status_code == 404
        assert http.get(bridge.origin + "/start", headers=headers).status_code == 404
        assert (
            http.post(bridge.origin + "/start", headers=headers, json={"command": "not allowed"}).status_code
            == 422
        )
        assert http.post(bridge.origin + "/stdin", headers=headers, json={}).status_code == 404
        assert http.post(bridge.origin + "/start", headers=headers, content="{}").status_code == 415
        assert (
            http.post(
                bridge.origin + "/start",
                headers={**headers, "Content-Type": "application/json"},
                content=b"x" * 257,
            ).status_code
            == 413
        )
        assert process.snapshot()["generation"] == 0
        assert bridge.token not in client.snapshot()["text"]


@pytest.mark.parametrize(
    "origin",
    [
        "https://real.invalid",
        "http://localhost:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://u:p@127.0.0.1:8",
        "http://127.0.0.1:8765/x",
        "http://127.0.0.1:8765?x=1",
    ],
)
def test_bridge_refuses_non_exact_loopback_origin(origin):
    with pytest.raises(ValueError):
        checked_origin(origin)


def test_large_unicode_snapshot_is_not_truncated_json():
    output = "東京" * 40000
    process = OwnedProcessMonitor(
        (sys.executable, "-u", "-c", "print('東京' * 40000)"), max_line_chars=90000, max_chars=100000
    )
    bridge = MonitorBridge(process, "http://127.0.0.1:8765")
    bridge.start()
    try:
        process.start().result(5)
        until(lambda: process.snapshot()["state"] == "exited")
        result = BridgeClient(bridge.origin, bridge.token).snapshot()
        assert output in result["text"]
        assert result["error"] is None
    finally:
        bridge.close()
        process.close()
