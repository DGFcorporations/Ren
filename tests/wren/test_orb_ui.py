"""Tests for Wren Floating Orb & Draft Preview Web Server."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from wren.core.controller import WrenController
from wren.core.state import WrenState
from wren.targets.x11 import TargetWindow
from wren.ui.orb_preview import create_app


@pytest.fixture
def mock_controller():
    controller = WrenController(stt_provider="mock", model="auto")
    controller.active_target = TargetWindow(
        window_id="12345",
        wm_class="gedit",
        title="Notes",
        pid=1000,
        target_hash="hash-12345",
        is_terminal=False,
    )
    return controller


def test_index_page(mock_controller):
    app = create_app(mock_controller)
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "Wren Assistant" in res.text
    assert "draft-box" in res.text


def test_status_endpoint(mock_controller):
    app = create_app(mock_controller)
    client = TestClient(app)
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "IDLE"
    assert data["active_target"]["wm_class"] == "gedit"


def test_listen_and_elevate_endpoints(mock_controller):
    app = create_app(mock_controller)
    client = TestClient(app)
    
    with patch.object(mock_controller.audio, "start"):
        res = client.post("/api/listen/start")
        assert res.status_code == 200
        assert res.json()["status"] == "listening"

    with patch.object(mock_controller.audio, "stop", return_value="/tmp/test.wav"), \
         patch.object(mock_controller.stt, "transcribe", return_value="send update to bob"), \
         patch.object(mock_controller.elevator, "elevate") as mock_elev:
        
        mock_elev.return_value = MagicMock(
            raw_text="send update to bob",
            elevated_text="Please send the update to Bob.",
            diff="[+Please send the update to Bob.]",
            intent_preserved=True,
            model="auto",
            fallback_used=False,
        )
        res = client.post("/api/listen/stop")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "draft_ready"
        assert data["revision"] == 1
        assert "Bob" in data["text"]


def test_revise_and_revert_endpoints(mock_controller):
    app = create_app(mock_controller)
    client = TestClient(app)
    
    # Setup initial draft
    u = mock_controller.buffer.record_utterance("raw speech")
    mock_controller.buffer.create_draft_from_utterance(u.id, "Initial text.")
    
    with patch.object(mock_controller.elevator, "elevate") as mock_elev:
        mock_elev.return_value = MagicMock(
            raw_text="make it brief",
            elevated_text="Brief text.",
            diff="[-Initial +Brief]",
            intent_preserved=True,
            model="auto",
            fallback_used=False,
        )
        res = client.post("/api/revise", json={"instruction": "make it brief"})
        assert res.status_code == 200
        assert res.json()["revision"] == 2
        assert res.json()["text"] == "Brief text."

    # Revert to raw
    res_rev = client.post("/api/revert")
    assert res_rev.status_code == 200
    assert res_rev.json()["text"] == "raw speech"


def test_copy_and_paste_endpoints(mock_controller):
    app = create_app(mock_controller)
    client = TestClient(app)
    
    u = mock_controller.buffer.record_utterance("meeting notes")
    mock_controller.buffer.create_draft_from_utterance(u.id, "Meeting Notes.")
    mock_controller.state_machine.transition(WrenState.DRAFT_READY)
    
    with patch.object(mock_controller.clipboard, "write", return_value="hash-123"):
        res_copy = client.post("/api/copy")
        assert res_copy.status_code == 200
        assert res_copy.json()["status"] == "copied"

    paste_ret = {"status": "pasted", "shortcut": "ctrl+v"}
    tgt = mock_controller.active_target
    with (
        patch.object(mock_controller.targets, "paste_into_target", return_value=paste_ret),
        patch.object(mock_controller.targets, "get_active_target", return_value=tgt),
        patch.object(mock_controller.clipboard, "safe_restore", return_value=True),
    ):
        res_paste = client.post("/api/paste")
        assert res_paste.status_code == 200
        assert res_paste.json()["status"] == "pasted"
        assert res_paste.json()["outcome"] == "done"


def test_floating_plasma_orb_ui_and_voice_endpoints(mock_controller):
    events = []

    class FakeWindowHooks:
        def set_hit_region(self, mode: str) -> None:
            events.append(("region", mode))

        def move_by(self, dx: int, dy: int) -> None:
            events.append(("move", dx, dy))

        def set_focusable(self, accept: bool) -> None:
            events.append(("focus", accept))

    app = create_app(mock_controller, window_hooks=FakeWindowHooks())
    client = TestClient(app)

    # Transparent floating plasma orb with radial menu, no rectangular card chrome
    html = client.get("/").text
    assert "plasma-canvas" in html
    assert "radial-menu" in html

    # Status exposes live voice_level and TTS state
    st = client.get("/api/status").json()
    assert "voice_level" in st
    assert st["tts_provider"] == "qwen"

    # Switch TTS provider between qwen and deepgram
    vc = client.post("/api/voice/config", json={"provider": "deepgram", "muted": False})
    assert vc.status_code == 200
    assert vc.json()["provider"] == "deepgram"

    # Start listening with explicit 'talk' intent
    with patch.object(mock_controller.audio, "start"):
        r = client.post("/api/listen/start", json={"intent": "talk"})
        assert r.status_code == 200
        assert mock_controller._listen_intent == "talk"
        mock_controller.cancel()

    # Window hit-region and drag-move hooks
    assert client.post("/api/ui/hitregion", json={"mode": "expanded"}).status_code == 200
    assert client.post("/api/ui/move", json={"dx": 12, "dy": -8}).status_code == 200
    assert ("region", "expanded") in events
    assert ("move", 12, -8) in events
