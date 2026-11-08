"""Tests for Wren Mobile-to-Workstation Connective Bridge & Mode APIs."""
from unittest.mock import MagicMock, patch

import pytest
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
