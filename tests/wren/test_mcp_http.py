"""Streamable-HTTP MCP transport mounted at /mcp (contract: docs/MCP_SERVER.md)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from wren.core.controller import WrenController
from wren.mcp import server
from wren.mcp.server import TOOLS
from wren.ui.orb_preview import create_app

try:
    import mcp  # noqa: F401

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

pytestmark = pytest.mark.skipif(not MCP_AVAILABLE, reason="mcp package not installed")

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _initialize(client: TestClient) -> str:
    res = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    assert res.status_code == 200, res.text
    session_id = res.headers.get("mcp-session-id")
    assert session_id
    client.post(
        "/mcp",
        headers={**MCP_HEADERS, "mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return session_id


def test_mcp_initialize_and_tools_list():
    controller = WrenController(stt_provider="mock")
    with TestClient(create_app(controller)) as client:
        session_id = _initialize(client)
        res = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "mcp-session-id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        names = [t["name"] for t in body["result"]["tools"]]
        assert names == [t["name"] for t in TOOLS]
        assert "ren_paste_commit" in names
        assert "ren_browser" in names


def test_mcp_tools_call_dispatches_and_maps_errors():
    controller = WrenController(stt_provider="mock")
    with TestClient(create_app(controller)) as client:
        session_id = _initialize(client)
        headers = {**MCP_HEADERS, "mcp-session-id": session_id}
        with patch("wren.mcp.server.dispatch", return_value={"ok": True, "state": "IDLE"}) as mocked:
            res = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "ren_status", "arguments": {}},
                },
            )
            assert res.status_code == 200, res.text
            result = res.json()["result"]
            assert result["isError"] is False
            payload = json.loads(result["content"][0]["text"])
            assert payload == {"ok": True, "state": "IDLE"}
            mocked.assert_called_once_with("ren_status", {})

        res_err = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "ren_nope", "arguments": {}},
            },
        )
        assert res_err.status_code == 200, res_err.text
        err_result = res_err.json()["result"]
        assert err_result["isError"] is True
        assert "Unknown tool" in err_result["content"][0]["text"]


def test_mcp_requires_auth_for_proxied_requests():
    controller = WrenController(stt_provider="mock")
    app = create_app(controller, auth_token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # Direct non-loopback host without token -> 401 before MCP handling
        res = client.post("/mcp", headers={**MCP_HEADERS, "host": "203.0.113.10"}, json={})
        assert res.status_code == 401
        # Loopback peer but proxy-forwarded headers -> treated as non-loopback -> 401
        res_fwd = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "x-forwarded-for": "203.0.113.10"},
            json={},
        )
        assert res_fwd.status_code == 401
        # Same forwarded headers with valid bearer -> passes auth (protocol error is fine, not 401)
        res_ok = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "x-forwarded-for": "203.0.113.10", "authorization": "Bearer s3cret"},
            json={},
        )
        assert res_ok.status_code != 401


def test_status_reports_mcp_http_mounted():
    controller = WrenController(stt_provider="mock")
    with TestClient(create_app(controller)) as client:
        body = client.get("/api/status").json()
        assert body["mcp_http_mounted"] is True


def _tools_call(client: TestClient, session_id: str, msg_id: int, name: str, arguments: dict):
    res = client.post(
        "/mcp",
        headers={**MCP_HEADERS, "mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )
    assert res.status_code == 200, res.text
    return res.json()["result"]


def test_mcp_tools_call_runs_locally_without_http_self_call(tmp_path, monkeypatch):
    """Local dispatch: MCP tools run against the live controller, never HTTP.

    The deployed failure was Cloudflare 1010 on the MCP tools' HTTP self-call
    to /api/*. With the controller registered by the transport, dispatch()
    must not call _request() at all — patch it to explode if it is reached.
    """
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    (tmp_path / "hello.txt").write_text("hi")
    controller = WrenController(stt_provider="mock")
    with TestClient(create_app(controller)) as client:
        session_id = _initialize(client)
        with patch("wren.mcp.server._request", side_effect=AssertionError("HTTP self-call made")):
            status = _tools_call(client, session_id, 10, "ren_status", {})
            assert status["isError"] is False
            payload = json.loads(status["content"][0]["text"])
            assert payload["mcp_http_mounted"] is True
            assert payload["state"] == "IDLE"

            screen = _tools_call(client, session_id, 11, "ren_screen_read", {"source": "desktop"})
            assert screen["isError"] is False
            desktop = json.loads(screen["content"][0]["text"])["desktop"]
            assert "app_name" in desktop and "window_title" in desktop

            files = _tools_call(client, session_id, 12, "ren_files", {"op": "list", "path": "."})
            assert files["isError"] is False
            listed = json.loads(files["content"][0]["text"])
            assert "hello.txt" in listed["files"]


def test_dispatch_local_status_matches_rest_status_shape(tmp_path, monkeypatch):
    """Local ren_status returns exactly the same keys as GET /api/status."""
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    controller = WrenController(stt_provider="mock")
    with TestClient(create_app(controller)) as client:
        rest = client.get("/api/status").json()
        with patch("wren.mcp.server._request", side_effect=AssertionError("HTTP self-call made")):
            token = server.local_controller.set(controller)
            try:
                local = server.dispatch("ren_status", {})
            finally:
                server.local_controller.reset(token)
        assert set(local) == set(rest)
        assert local["mcp_http_mounted"] is True


def test_dispatch_without_local_controller_still_uses_http():
    """Stdio bridge (no local controller) still forwards to the loopback API."""
    with patch("wren.mcp.server._request", return_value={"ok": True}) as mocked:
        result = server.dispatch("ren_status", {})
    mocked.assert_called_once_with("GET", "/api/status")
    assert result == {"ok": True}


def test_local_dispatch_calls_controller_for_llm_tools():
    """ren_converse/ren_dictate reach the controller directly (no HTTP)."""
    controller = MagicMock()
    controller.converse.return_value = "hello back"
    draft = MagicMock()
    draft.revision = 3
    draft.text = "dictated"
    controller.accept_dictation.return_value = draft
    token = server.local_controller.set(controller)
    try:
        with patch("wren.mcp.server._request", side_effect=AssertionError("HTTP self-call made")):
            out = server.dispatch("ren_converse", {"text": "hi"})
            assert out == {"type": "assistant_message", "text": "hello back", "requires_confirm": True}
            controller.converse.assert_called_once_with("hi", source="orb")
            out = server.dispatch("ren_dictate", {"text": "take this down"})
            assert out["status"] == "draft_ready"
            assert out["revision"] == 3
            assert out["text"] == "dictated"
            controller.accept_dictation.assert_called_once_with("take this down", source="mcp")
    finally:
        server.local_controller.reset(token)


def test_local_dispatch_keeps_confirm_gates(tmp_path, monkeypatch):
    """Confirm gates and validation survive the local path unchanged."""
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    controller = WrenController(stt_provider="mock")
    token = server.local_controller.set(controller)
    try:
        with pytest.raises(server.MCPError, match="confirm true"):
            server.dispatch("ren_paste_commit", {})
        with pytest.raises(server.MCPError, match="Invalid mode"):
            server.dispatch("ren_set_mode", {"mode": "nope"})
        preview = server.dispatch(
            "ren_files", {"op": "write", "path": "x.txt", "content": "hi"}
        )
        assert preview["requires_confirm"] is True
        assert preview["committed"] is False
        assert not (tmp_path / "x.txt").exists()
        assert server.dispatch("ren_set_mode", {"mode": "agent"}) == {
            "status": "ok", "mode": "agent",
        }
    finally:
        server.local_controller.reset(token)
