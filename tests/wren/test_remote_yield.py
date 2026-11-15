"""Remote-agent session yield (agent drives → local surfaces drop) and
post-STT term corrections ("Rin"/"Wren" → "Ren")."""

import time
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from wren.core.controller import WrenController
from wren.core.state import WrenState
from wren.stt.correct import apply_corrections, load_corrections
from wren.ui.orb_preview import create_app

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _initialize(client: TestClient) -> str:
    res = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}},
    )
    assert res.status_code == 200, res.text
    return res.headers["mcp-session-id"]


# ---------------- term corrections ----------------

def test_corrections_fix_ren_misspellings():
    assert apply_corrections("ask rin to do it") == "ask Ren to do it"
    assert apply_corrections("tell Wren the plan") == "tell Ren the plan"
    assert apply_corrections("Rinn said hi") == "Ren said hi"
    assert apply_corrections("rynn") == "rynn" or apply_corrections("ryn") == "Ren"


def test_corrections_possessive_and_code_tokens():
    assert apply_corrections("that's Rin's job") == "that's Ren's job"
    assert apply_corrections("restart wren.service") == "restart wren.service"
    assert apply_corrections("edit wren.cli now") == "edit wren.cli now"


def test_corrections_leave_other_words_alone():
    text = "bring the ring and grin at the string"
    assert apply_corrections(text) == text


def test_corrections_env_override(monkeypatch):
    monkeypatch.setenv("WREN_STT_CORRECTIONS", "devon:Devin")
    table = load_corrections()
    assert table["devon"] == "Devin"
    assert apply_corrections("tell devon thanks", table) == "tell Devin thanks"
    # defaults still present
    assert apply_corrections("rin", table) == "Ren"


# ---------------- remote-agent yield lease ----------------

def test_remote_session_blocks_local_actions_and_releases(tmp_path, monkeypatch):
    """Full lifecycle: MCP request sets the lease → local mutating REST 409s →
    release endpoint clears it → local actions work again."""
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    controller = WrenController(stt_provider="mock")
    assert not controller.remote_session_active

    with TestClient(create_app(controller)) as client:
        _initialize(client)  # any /mcp request extends the lease
        assert controller.remote_session_active

        # Monitoring stays up — status is a GET.
        status = client.get("/api/status").json()
        assert status["remote_session"]["active"] is True
        assert status["remote_session"]["ttl_s"] > 0

        # Local mutating actions yield with 409.
        res = client.post("/api/dictate", json={"text": "hello"})
        assert res.status_code == 409
        assert res.json()["error"] == "remote_agent_session"
        assert client.post("/api/converse", json={"text": "hi"}).status_code == 409
        assert client.post("/api/mode", json={"mode": "agent"}).status_code == 409

        # Escape hatches stay open.
        assert client.post("/api/conversation/clear").status_code == 200

        # MCP tools keep working during the lease.
        sid = _initialize(client)
        res = client.post(
            "/mcp", headers={**MCP_HEADERS, "mcp-session-id": sid},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "ren_status", "arguments": {}}},
        )
        assert res.json()["result"]["isError"] is False

        # User kill-switch returns control.
        res = client.post("/api/session/release")
        assert res.status_code == 200
        assert not controller.remote_session_active
        assert client.post("/api/mode", json={"mode": "agent"}).status_code == 200


def test_lease_expires_on_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    controller = WrenController(stt_provider="mock")
    controller.remote_session_ttl = 0.05
    controller.touch_remote_session()
    assert controller.remote_session_active
    time.sleep(0.08)
    assert not controller.remote_session_active


def test_hotkey_listen_yields_during_remote_session(tmp_path, monkeypatch):
    """The pynput hotkey path bypasses REST — the controller guard covers it."""
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    controller = WrenController(stt_provider="mock")
    controller.touch_remote_session()
    controller.start_listening(intent="dictate")
    assert controller.state == WrenState.IDLE
    assert not controller.audio.is_recording


def test_ui_geometry_not_blocked_during_session(tmp_path, monkeypatch):
    """Orb hitregion/move calls must stay live or the orb freezes visually."""
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    controller = WrenController(stt_provider="mock")
    hooks = MagicMock()
    with TestClient(create_app(controller, window_hooks=hooks)) as client:
        controller.touch_remote_session()
        res = client.post("/api/ui/hitregion", json={"region": "orb"})
        assert res.status_code == 200
