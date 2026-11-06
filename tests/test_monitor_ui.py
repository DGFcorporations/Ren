"""Actual Streamlit UI + real owned Ren stdout; no provider or native-GUI validation."""

from __future__ import annotations

import importlib.util
import re
import socket
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from workbench_pilot.monitor_launcher import MonitorRuntime

pytestmark = [pytest.mark.monitor, pytest.mark.browser]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError("Local monitor test condition timed out")


@pytest.fixture
def monitor_ui(tmp_path):
    if importlib.util.find_spec("streamlit") is None:
        pytest.fail("Install monitor dependencies to run the full suite; use -m 'not monitor' for core only")
    ren_port, monitor_port = free_port(), free_port()
    while monitor_port == ren_port:
        monitor_port = free_port()
    runtime = MonitorRuntime(ren_port, monitor_port, tmp_path / "synthetic-data")
    try:
        runtime.start(timeout=30)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, chromium_sandbox=True)
            context = browser.new_context(service_workers="block", viewport={"width": 1320, "height": 1000})
            outbound = []

            def route_request(route):
                if route.request.url.startswith(runtime.url + "/"):
                    route.continue_()
                else:
                    outbound.append(route.request.url)
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(runtime.url)
            expect(page.get_by_label("Ren stdout + stderr")).to_be_visible(timeout=20000)
            yield page, runtime
            assert runtime.bridge.token not in page.content()
            assert outbound == [], f"Unexpected external UI requests: {outbound}"
            assert errors == [], f"JavaScript errors: {errors}"
            context.close()
            browser.close()
    finally:
        runtime.close()
        if runtime.ren:
            assert runtime.ren.snapshot()["state"] == "closed"
        if runtime.dashboard:
            assert runtime.dashboard.snapshot()["state"] == "closed"


def test_stdout_updates_without_reload_and_lifecycle_controls(monitor_ui):
    page, runtime = monitor_ui
    output = page.get_by_label("Ren stdout + stderr")
    expect(output).to_have_value(re.compile("G1 OFFLINE"), timeout=15000)
    expect(output).to_be_disabled()
    pid = runtime.ren.snapshot()["pid"]
    assert runtime.ren.snapshot()["generation"] == 1

    # Generate real sanitized output in the owned Ren subprocess through an
    # approved synthetic memory proposal. No provider calls or real recipients.
    with httpx.Client(base_url=f"http://127.0.0.1:{runtime.ren_port}", trust_env=False, timeout=5) as client:
        html = client.get("/").text
        client.headers["X-Pilot-CSRF"] = re.search(r'name="pilot-csrf" content="([0-9a-f]+)"', html)[1]
        row = client.post(
            "/api/pilot/memory/proposals",
            json={
                "request_id": "monitor-ui-test",
                "key": "synthetic.monitor",
                "value": "SYNTHETIC-PAYLOAD-NOT-FOR-LOGS",
            },
        ).json()
        started = time.perf_counter()
        expect(output).to_have_value(re.compile("memory_proposal_ready"), timeout=10000)
        update_delay = time.perf_counter() - started
        action = row["proposal"]
        assert (
            client.post(
                "/api/pilot/approvals/" + action["id"], json={"approved": False, "digest": action["digest"]}
            ).status_code
            == 200
        )
        expect(output).to_have_value(re.compile('"state":"denied"'), timeout=10000)
    assert "SYNTHETIC-PAYLOAD-NOT-FOR-LOGS" not in output.input_value()
    assert runtime.ren.snapshot()["pid"] == pid
    artifacts = Path(__file__).resolve().parents[1] / "test-artifacts"
    artifacts.mkdir(exist_ok=True)
    page.screenshot(path=str(artifacts / "stdout-monitor.png"), full_page=False)
    (artifacts / "stdout-update-delay.txt").write_text(f"Observed single local update: {update_delay:.4f}s\n")

    page.reload()
    expect(page.get_by_label("Ren stdout + stderr")).to_have_value(re.compile("memory_proposal_ready"))
    assert runtime.ren.snapshot()["pid"] == pid
    assert runtime.ren.snapshot()["generation"] == 1

    page.get_by_role("button", name="Stop Ren", exact=True).click()
    expect(page.get_by_role("button", name="Start Ren", exact=True)).to_be_enabled(timeout=15000)
    assert runtime.ren.snapshot()["state"] == "exited"
    page.get_by_role("button", name="Clear view", exact=True).click()
    expect(page.get_by_label("Ren stdout + stderr")).to_have_value("")
    page.get_by_role("button", name="Start Ren", exact=True).click()
    expect(page.get_by_label("Ren stdout + stderr")).to_have_value(re.compile("G1 OFFLINE"), timeout=15000)
    assert runtime.ren.snapshot()["generation"] == 2


def test_lost_bridge_is_visible_and_never_respawns_a_child(monitor_ui):
    page, runtime = monitor_ui
    expect(page.get_by_label("Ren stdout + stderr")).to_have_value(re.compile("G1 OFFLINE"), timeout=15000)
    pid = runtime.ren.snapshot()["pid"]
    runtime.bridge.close()
    expect(page.get_by_text("The local control bridge is unavailable.", exact=False)).to_be_visible(
        timeout=10000
    )
    assert runtime.ren.snapshot()["pid"] == pid
    assert runtime.ren.snapshot()["generation"] == 1


def test_streamlit_rejects_foreign_host_and_origin(monitor_ui):
    page, runtime = monitor_ui
    headers = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Protocol": "streamlit",
    }
    with httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(
            runtime.url + "/_stcore/stream",
            headers={**headers, "Host": "foreign.invalid:8501", "Origin": "http://foreign.invalid:8501"},
        )
        assert response.status_code in {400, 403}
        response = client.get(
            runtime.url + "/_stcore/stream", headers={**headers, "Origin": "https://foreign.invalid"}
        )
        assert response.status_code in {400, 403}
    assert runtime.ren.snapshot()["generation"] == 1
    expect(page.get_by_label("Ren stdout + stderr")).to_have_value(re.compile("G1 OFFLINE"))
