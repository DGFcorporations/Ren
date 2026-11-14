"""Ren agent bridge: dictation preview, talk, screen, files, browser guard, MCP."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from wren.bridge.workspace import WorkspaceError, WorkspaceWriter
from wren.browser.session import BrowserError, check_url
from wren.core.controller import WrenController
from wren.mcp.server import handle
from wren.ui.orb_preview import create_app


def test_dictation_stages_preview_and_does_not_paste():
    controller = WrenController(stt_provider="mock")
    app = create_app(controller)
    client = TestClient(app)
    res = client.post("/api/dictate", json={"text": "email dana about the lease", "source": "android"})
    assert res.status_code == 200
    body = res.json()
    assert body["type"] == "paste_preview"
    assert body["requires_confirm"] is True
    assert "Dana" in body["text"] or "dana" in body["text"].lower()
    assert controller.state.value == "DRAFT_READY"
    preview = client.get("/api/paste/preview").json()
    assert preview["draft"]["text"] == body["text"]


def test_converse_and_phone_screen_roundtrip():
    controller = WrenController(stt_provider="mock")
    app = create_app(controller)
    client = TestClient(app)
    screen = client.post("/api/screen/phone", json={"package": "com.android.mms", "text": "Draft to Dana"})
    assert screen.status_code == 200
    assert client.get("/api/screen/phone").json()["package"] == "com.android.mms"
    reply = client.post("/api/converse", json={"text": "what should I say about the lease"})
    assert reply.status_code == 200
    text = reply.json()["text"].lower()
    assert any(w in text for w in ("lease", "renewal", "dana", "draft", "reply"))
    assert reply.json()["requires_confirm"] is True


def test_websocket_dictation_returns_preview():
    controller = WrenController(stt_provider="mock")
    app = create_app(controller)
    client = TestClient(app)
    with client.websocket_connect("/ws/connect") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"action": "dictate", "text": "remind me to call the clinic", "screen": "Calendar"}
        )
        msg = websocket.receive_json()
        assert msg["type"] == "paste_preview"
        assert msg["requires_confirm"] is True
        assert controller.phone_screen["text"] == "Calendar"


def test_workspace_write_requires_confirm(tmp_path: Path):
    space = WorkspaceWriter(tmp_path)
    preview = space.write("notes/today.txt", "hello ren", confirm=False)
    assert preview["committed"] is False
    assert not (tmp_path / "notes" / "today.txt").exists()
    written = space.commit_write(preview["approval_id"])
    assert written["committed"] is True
    assert (tmp_path / "notes" / "today.txt").read_text() == "hello ren"
    try:
        space.write("../outside.txt", "nope", confirm=True)
        raise AssertionError("escape should fail")
    except WorkspaceError:
        pass
    try:
        space.write(".env", "SECRET=1", confirm=True)
        raise AssertionError("secret name should fail")
    except WorkspaceError:
        pass


def test_browser_rejects_non_http_before_launch():
    try:
        check_url("file:///etc/passwd")
        raise AssertionError("file url should fail")
    except BrowserError:
        pass
    try:
        check_url("javascript:alert(1)")
        raise AssertionError("javascript url should fail")
    except BrowserError:
        pass


def test_mcp_lists_tools_and_guards_paste():
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert "ren_converse" in names
    assert "ren_browser" in names
    assert "ren_files" in names
    assert "ren_screen_read" in names
    blocked = handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ren_paste_commit", "arguments": {}},
        }
    )
    assert blocked["result"]["isError"] is True
    assert "confirm" in blocked["result"]["content"][0]["text"]


def test_browser_act_without_confirm_does_not_launch():
    controller = WrenController(stt_provider="mock")
    controller.browser._ensure = MagicMock(side_effect=AssertionError("browser should not launch"))
    app = create_app(controller)
    client = TestClient(app)
    res = client.post("/api/browser/act", json={"op": "click", "x": 10, "y": 10, "confirm": False})
    assert res.status_code == 200
    body = res.json()
    assert body["committed"] is False
    assert body["requires_confirm"] is True
    controller.browser._ensure.assert_not_called()


def test_converse_uses_screen_note():
    controller = WrenController(stt_provider="mock")
    controller.note_phone_screen("com.notes", "Lease renewal")
    with patch.object(controller.elevator, "converse", wraps=controller.elevator.converse) as mocked:
        controller.converse("help me reply")
        assert "Lease renewal" in mocked.call_args.kwargs["screen_note"]
        assert controller.conversation[-1]["role"] == "assistant"


def test_converse_drops_stale_screen_note_and_old_history():
    """Phantom-context guard: stale context must not be presented as current."""
    controller = WrenController(stt_provider="mock")
    controller.phone_screen = {"package": "com.old", "text": "ANCIENT SCREEN", "at": 1.0}
    controller.conversation = [
        {"role": "user", "content": "OLD TURN", "at": 1.0},
        {"role": "assistant", "content": "OLD REPLY", "at": 1.0},
    ]
    with patch.object(controller.elevator, "converse", wraps=controller.elevator.converse) as mocked:
        controller.converse("fresh question")
        assert "ANCIENT SCREEN" not in mocked.call_args.kwargs["screen_note"]
        passed_history = mocked.call_args.kwargs["history"]
        assert all("OLD TURN" not in str(t.get("content", "")) for t in passed_history)


def test_stt_artifact_filter_drops_noise_hallucinations(tmp_path):
    """Grounding gate: phantom transcripts never become drafts or turns."""
    controller = WrenController(stt_provider="mock")
    import io
    import wave

    def fake_wav(seconds: float) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * int(16000 * seconds))
        return buf.getvalue()

    # 0.4s capture + classic Whisper artifact on low energy -> dropped
    controller.audio.peak_level = 0.01
    assert controller._is_stt_artifact("Thank you.", fake_wav(0.4)) is True
    # Near-silent capture + short transcript -> dropped
    assert controller._is_stt_artifact("ok", fake_wav(2.0)) is True
    # Real speech: decent energy + real sentence -> kept
    controller.audio.peak_level = 0.4
    assert controller._is_stt_artifact("Tell Dana the lease is signed", fake_wav(3.0)) is False
    # Longer real clip saying a short phrase at real energy -> kept
    assert controller._is_stt_artifact("Thank you.", fake_wav(2.5)) is False
