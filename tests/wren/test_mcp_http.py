"""Streamable-HTTP MCP transport mounted at /mcp (contract: docs/MCP_SERVER.md)."""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from wren.core.controller import WrenController
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
