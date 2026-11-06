"""Real loopback HTTP + managed Chromium workflow tests; no external accounts/providers."""

from __future__ import annotations

import re
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from workbench_pilot.controller import OWNER
from workbench_pilot.policy import PilotSettings
from workbench_pilot.web import create_app


@contextmanager
def live_app(data_dir, *, ttl=300, faults=True):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = PilotSettings(data_dir, origin, approval_ttl_seconds=ttl, allow_fault_injection=faults)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "Local test server failed to start"
    try:
        with httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
            page = client.get("/")
            assert page.status_code == 200
            csrf = re.search(r'name="pilot-csrf" content="([0-9a-f]+)"', page.text)[1]
            client.headers["X-Pilot-CSRF"] = csrf
            yield client, app, origin
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        sock.close()
        assert not thread.is_alive(), "Test server did not shut down cleanly"


@pytest.fixture
def service(tmp_path):
    with live_app(tmp_path / "data") as value:
        yield value


def start(client, fixture="kickoff", surface="textarea", request_id="test-one"):
    response = client.post(
        "/api/pilot/runs", json={"request_id": request_id, "fixture_id": fixture, "surface": surface}
    )
    assert response.status_code == 200, response.text
    return response.json()


def wait(client, run_id, target="pending"):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get("/api/pilot/runs/" + run_id)
        assert response.status_code == 200, response.text
        row = response.json()
        if row["status"] == target:
            return row
        if row["status"] in {"failed", "uncertain", "cancelled", "stale", "expired", "denied", "completed"}:
            pytest.fail(f"Expected {target}, got {row['status']}: {row.get('reason')}")
        time.sleep(0.025)
    pytest.fail(f"Timed out waiting for {target}")


def decision(client, row, approved=True, digest=None):
    action = row["proposal"]
    return client.post(
        "/api/pilot/approvals/" + action["id"],
        json={"approved": approved, "digest": digest or action["digest"]},
    )


def test_loopback_session_csrf_and_status(service):
    client, app, origin = service
    state = client.get("/api/pilot/status").json()
    assert state["mode"] == "offline"
    assert state["authorized_provider_spend_usd"] == 0
    assert "UNVERIFIED" in state["speech_recognition"]
    assert "microphone=()" in client.get("/").headers["Permissions-Policy"]
    assert client.get("/assets/pilot.js").status_code == 200
    assert client.get("/assets/pilot.css").status_code == 200
    assert client.get("/assets/../web.py").status_code == 404
    with httpx.Client(base_url=origin, trust_env=False) as outsider:
        assert outsider.get("/api/pilot/status").status_code == 401
    body = {"request_id": "csrf", "fixture_id": "kickoff"}
    assert client.post("/api/pilot/runs", json=body, headers={"X-Pilot-CSRF": "wrong"}).status_code == 403
    assert (
        client.post("/api/pilot/runs", json=body, headers={"Origin": "https://foreign.invalid"}).status_code
        == 403
    )
    assert client.get("/", headers={"Host": "foreign.invalid"}).status_code == 400
    assert client.post("/api/pilot/runs", json={**body, "url": "https://real.invalid"}).status_code == 422
    assert client.post("/api/pilot/live/llm", json={}).status_code == 403
    assert client.post("/api/pilot/live/stt", json={}).status_code == 403
    assert client.post("/api/pilot/live/mem0", json={}).status_code == 403
    assert client.get("/api/pilot/tone.wav").content.startswith(b"RIFF")


def test_request_body_limits_including_chunked(service):
    client, _, _ = service
    assert (
        client.post(
            "/api/pilot/runs", content=b"x" * 65537, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )
    chunks = (b"x" * 32000 for _ in range(3))
    assert (
        client.post(
            "/api/pilot/runs", content=chunks, headers={"Content-Type": "application/json"}
        ).status_code
        == 413
    )


@pytest.mark.browser
@pytest.mark.parametrize("surface", ["textarea", "contenteditable", "iframe", "delayed"])
@pytest.mark.parametrize("fixture", ["kickoff", "checklist", "unicode"])
def test_real_approved_workflow_and_replay_protection(service, surface, fixture):
    client, app, _ = service
    row = wait(client, start(client, fixture, surface)["id"])
    initial = app.state.pilot.browser.snapshot(row["id"])
    assert initial["save_count"] == 0
    assert initial["before_text"] == ""
    expected = row["proposal"]["args"]["text"]
    if fixture == "unicode":
        assert "東京" in expected
    assert row["simulation"]["transcription"] == "scripted_fixture_not_STT"
    assert decision(client, row).status_code == 200
    done = wait(client, row["id"], "completed")
    assert done["receipt"]["verified"] is True
    assert done["receipt"]["content"] == expected
    assert done["receipt"]["save_count"] == 1
    assert done["provider_cost_usd"] == 0
    assert decision(client, row).status_code == 409
    duplicate = start(client, fixture, surface)
    assert duplicate["id"] == row["id"]
    assert duplicate["receipt"]["save_count"] == 1
    assert (
        client.post(
            "/api/pilot/runs",
            json={"request_id": "test-one", "fixture_id": "unicode" if fixture == "kickoff" else "kickoff"},
        ).status_code
        == 409
    )
    events = client.get(f"/api/pilot/runs/{row['id']}/events").json()
    assert [e["event"] for e in events].count("action_started") == 1
    preview = client.get(f"/api/pilot/runs/{row['id']}/preview.png")
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")


@pytest.mark.browser
@pytest.mark.parametrize("operation", ["deny", "cancel", "bad_digest"])
def test_refused_actions_never_start(service, operation):
    client, app, _ = service
    row = wait(client, start(client)["id"])
    assert app.state.pilot.browser.snapshot(row["id"])["save_count"] == 0
    if operation == "deny":
        assert decision(client, row, False).status_code == 200
        expected = "denied"
    elif operation == "cancel":
        assert client.post(f"/api/pilot/runs/{row['id']}/cancel", json={}).status_code == 200
        expected = "cancelled"
    else:
        assert decision(client, row, digest="0" * 64).status_code == 409
        expected = "stale"
    stopped = wait(client, row["id"], expected)
    assert stopped["receipt"] is None
    assert decision(client, row).status_code == 409
    events = client.get(f"/api/pilot/runs/{row['id']}/events").json()
    assert all(e["event"] != "action_started" for e in events)


@pytest.mark.browser
@pytest.mark.parametrize("fault", ["recipient", "draft", "reload", "memory", "close"])
def test_changed_context_invalidates_approval(service, fault):
    client, _, _ = service
    row = wait(client, start(client)["id"])
    assert client.post(f"/api/pilot/runs/{row['id']}/fault", json={"kind": fault}).status_code == 200
    assert decision(client, row).status_code == 200
    done = wait(client, row["id"], "failed" if fault == "close" else "stale")
    assert done["receipt"] is None
    events = client.get(f"/api/pilot/runs/{row['id']}/events").json()
    assert all(e["event"] != "action_started" for e in events)


def test_memory_writes_require_approval_and_delete_stays_deleted(service):
    client, app, _ = service
    key = "synthetic.disposable"
    body = {"request_id": "memory-add", "key": key, "value": "Synthetic memory value"}
    row = client.post("/api/pilot/memory/proposals", json=body).json()
    assert row["status"] == "pending"
    assert app.state.pilot.ledger.get_fact(OWNER, key) is None
    assert decision(client, row).status_code == 200
    wait(client, row["id"], "completed")
    assert app.state.pilot.ledger.get_fact(OWNER, key)["value"] == "Synthetic memory value"
    deletion = client.post(
        "/api/pilot/memory/proposals", json={"request_id": "memory-delete", "key": key, "delete": True}
    ).json()
    assert app.state.pilot.ledger.get_fact(OWNER, key) is not None
    decision(client, deletion)
    wait(client, deletion["id"], "completed")
    assert app.state.pilot.ledger.get_fact(OWNER, key) is None
    app.state.pilot.ledger.seed_facts(OWNER, {key: "must not resurrect"})
    assert app.state.pilot.ledger.get_fact(OWNER, key) is None
    assert client.get("/api/pilot/facts", params={"q": "disposable"}).json()["facts"] == []
    assert (
        client.post(
            "/api/pilot/memory/proposals", json={"request_id": "bad", "key": "real_account", "value": "x"}
        ).status_code
        == 422
    )


def test_sessions_cannot_approve_each_others_action(service):
    client, _, origin = service
    row = client.post(
        "/api/pilot/memory/proposals",
        json={"request_id": "session-test", "key": "synthetic.new", "value": "v"},
    ).json()
    with httpx.Client(base_url=origin, trust_env=False) as other:
        html = other.get("/").text
        other.headers["X-Pilot-CSRF"] = re.search(r'name="pilot-csrf" content="([0-9a-f]+)"', html)[1]
        assert other.get("/api/pilot/runs/" + row["id"]).status_code == 404
        assert decision(other, row).status_code == 404
    assert client.get("/api/pilot/runs/" + row["id"]).json()["status"] == "pending"


def test_restart_invalidates_pending_approval_and_preserves_facts(tmp_path):
    data = tmp_path / "data"
    with live_app(data) as (client, app, _):
        row = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "restart", "key": "synthetic.new", "value": "v"},
        ).json()
        old_id = row["id"]
        assert row["status"] == "pending"
    with live_app(data) as (client, app, _):
        old = app.state.pilot.ledger.get_run(OWNER, old_id)
        # Graceful shutdown cancels; an abrupt process death would recover to interrupted.
        assert old["status"] in {"cancelled", "interrupted"}
        assert app.state.pilot.ledger.get_fact(OWNER, "synthetic.helix.kickoff") is not None
        assert decision(client, row).status_code in {404, 409}
        assert old["receipt"] is None


def test_approval_expires_durably(tmp_path):
    with live_app(tmp_path / "expiry", ttl=0.05) as (client, app, _):
        row = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "expiry", "key": "synthetic.expiry", "value": "never"},
        ).json()
        time.sleep(0.06)
        assert decision(client, row).status_code == 409
        assert client.get("/api/pilot/runs/" + row["id"]).json()["status"] == "expired"
        assert app.state.pilot.ledger.get_fact(OWNER, "synthetic.expiry") is None


@pytest.mark.browser
def test_error_after_real_save_is_uncertain_not_retried(service):
    client, app, _ = service
    row = wait(client, start(client)["id"])
    execute = app.state.pilot.browser.execute
    observed = []

    def lost_response(*args, **kwargs):
        observed.append(execute(*args, **kwargs))
        raise RuntimeError("Synthetic loss of receipt after save")

    app.state.pilot.browser.execute = lost_response
    assert decision(client, row).status_code == 200
    result = wait(client, row["id"], "uncertain")
    assert observed[0]["save_count"] == 1
    assert result["receipt"] is None
    assert decision(client, row).status_code == 409
    assert start(client)["status"] == "uncertain"
    assert len(observed) == 1


@pytest.mark.browser
def test_cancel_after_execution_begins_does_not_claim_undo(service):
    client, app, _ = service
    row = wait(client, start(client)["id"])
    entered, release = threading.Event(), threading.Event()
    execute = app.state.pilot.browser.execute

    def controlled_delay(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return execute(*args, **kwargs)

    app.state.pilot.browser.execute = controlled_delay
    assert decision(client, row).status_code == 200
    assert entered.wait(5)
    cancelled = client.post(f"/api/pilot/runs/{row['id']}/cancel", json={}).json()
    assert cancelled["cancel_requested"] is True
    assert cancelled["status"] == "executing"
    release.set()
    done = wait(client, row["id"], "completed")
    assert done["cancel_requested"] is True
    assert done["receipt"]["save_count"] == 1
