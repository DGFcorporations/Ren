"""Tests for Wren Mobile-to-Workstation Connective Bridge & Mode APIs."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from wren.core.controller import WrenController
from wren.ui.orb_preview import create_app


@pytest.fixture
def test_controller():
    controller = WrenController(stt_provider="mock", model="auto")
    return controller


def test_get_connect_api(test_controller):
    app = create_app(test_controller)
    client = TestClient(app)

    res = client.get("/api/connect")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ready"
    assert "lan_ip" in data
    assert "tailscale_ip" in data
    assert data["port"] == 8765
    assert data["mode"] == "elevate"
    assert "/ws/connect" in data["endpoints"]["ws_connect"]


def test_post_mode_api(test_controller):
    app = create_app(test_controller)
    client = TestClient(app)

    # Switch to agent mode
    res = client.post("/api/mode", json={"mode": "agent"})
    assert res.status_code == 200
    assert res.json()["mode"] == "agent"
    assert test_controller.mode == "agent"

    # Switch to brainstorm mode
    res = client.post("/api/mode", json={"mode": "brainstorm"})
    assert res.status_code == 200
    assert res.json()["mode"] == "brainstorm"
    assert test_controller.mode == "brainstorm"

    # Invalid mode returns 400
    res_bad = client.post("/api/mode", json={"mode": "invalid_mode"})
    assert res_bad.status_code == 400


def test_websocket_connect_lifecycle(test_controller):
    app = create_app(test_controller)
    client = TestClient(app)

    with client.websocket_connect("/ws/connect") as websocket:
        # Handshake
        handshake = websocket.receive_json()
        assert handshake["type"] == "handshake"
        assert handshake["mode"] == "elevate"

        # Ping-pong
        websocket.send_json({"action": "ping"})
        pong = websocket.receive_json()
        assert pong["type"] == "pong"

        # Mode switch via WS
        websocket.send_json({"action": "set_mode", "mode": "agent"})
        mode_msg = websocket.receive_json()
        assert mode_msg["type"] == "mode_updated"
        assert mode_msg["mode"] == "agent"
        assert test_controller.mode == "agent"

        # Elevate text directly via WS
        with patch.object(test_controller.elevator, "elevate") as mock_elev:
            mock_elev.return_value = MagicMock(
                raw_text="build the app",
                elevated_text="# Directive: Build App",
                diff="[+directive]",
            )
            websocket.send_json({"action": "elevate_text", "text": "build the app"})
            elev_msg = websocket.receive_json()
            assert elev_msg["type"] == "elevated"
            assert elev_msg["elevated_text"] == "# Directive: Build App"


def test_nonloopback_auth_protection(test_controller):
    """Verify non-loopback requests without valid token are rejected with 401."""
    app = create_app(test_controller, auth_token="test-secret-token")
    client = TestClient(app)

    # 1. Loopback without token succeeds
    res_loopback = client.get("/api/connect")
    assert res_loopback.status_code == 200

    # 2. Non-loopback host without token returns 401
    res_unauth = client.get("/api/screen/capture", headers={"host": "203.0.113.10"})
    assert res_unauth.status_code == 401
    assert "Unauthorized" in res_unauth.json()["detail"]
    assert res_unauth.headers.get("www-authenticate") == "Bearer"

    # 3. Non-loopback read_file without token returns 401
    res_read_unauth = client.post(
        "/api/files/read",
        json={"file_path": "/home/chandler/Dev/README.md"},
        headers={"host": "203.0.113.10"},
    )
    assert res_read_unauth.status_code == 401

    # 4. Non-loopback with valid Bearer token succeeds
    res_bearer = client.get(
        "/api/screen/capture",
        headers={"host": "203.0.113.10", "authorization": "Bearer test-secret-token"},
    )
    assert res_bearer.status_code == 200

    # 5. Non-loopback with valid X-Wren-Token succeeds
    res_custom = client.get(
        "/api/screen/capture",
        headers={"host": "203.0.113.10", "x-wren-token": "test-secret-token"},
    )
    assert res_custom.status_code == 200

    # 6. Non-loopback with valid query param token succeeds
    res_query = client.get(
        "/api/screen/capture?token=test-secret-token",
        headers={"host": "203.0.113.10"},
    )
    assert res_query.status_code == 200

    # 7. Non-loopback with invalid token returns 401
    res_bad_token = client.get(
        "/api/screen/capture",
        headers={"host": "203.0.113.10", "authorization": "Bearer wrong-token"},
    )
    assert res_bad_token.status_code == 401


def test_nonloopback_websocket_auth(test_controller):
    """Verify non-loopback WebSockets require token."""
    app = create_app(test_controller, auth_token="test-secret-token")
    client = TestClient(app)

    # 1. Loopback WebSocket connects cleanly
    with client.websocket_connect("/ws/connect") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "handshake"

    # 2. Non-loopback WebSocket without token closes with 1008
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/connect", headers={"host": "203.0.113.10"}):
            pass
    assert exc_info.value.code == 1008

    # 3. Non-loopback WebSocket with query token succeeds
    with client.websocket_connect(
        "/ws/connect?token=test-secret-token",
        headers={"host": "203.0.113.10"},
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "handshake"
