"""Tests for CloudHybridSTT, NeuralTTS (Qwen -> Deepgram fallback), and two-way voice conversation."""
from unittest.mock import MagicMock, patch

import httpx

from wren.core.controller import WrenController
from wren.core.state import WrenState
from wren.stt.engine import CloudHybridSTT
from wren.tts.engine import NeuralTTS, clean_spoken_text


def test_clean_spoken_text_strips_markdown_and_code():
    raw = "Here is **important** info: `git status` and [link](https://example.com)."
    cleaned = clean_spoken_text(raw)
    assert "**" not in cleaned
    assert "`" not in cleaned
    assert "https://" not in cleaned
    assert "important" in cleaned


def test_cloud_hybrid_stt_uses_bridge_and_falls_back_to_local():
    fallback = MagicMock()
    fallback.transcribe.return_value = "local whisper fallback"
    stt = CloudHybridSTT(bridge_url="http://127.0.0.1:8787", fallback=fallback)

    # 1. Bridge returns real transcript
    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"text": "Hey Ren how is the build", "provider": "groq-whisper-ast"}
    with patch("httpx.Client.post", return_value=ok_resp):
        assert stt.transcribe(b"RIFF....WAVE") == "Hey Ren how is the build"
        fallback.transcribe.assert_not_called()

    # 2. Bridge returns silence sentinel -> returns empty string without hallucination
    sentinel_resp = MagicMock(status_code=200)
    sentinel_resp.json.return_value = {"text": "Voice command captured"}
    with patch("httpx.Client.post", return_value=sentinel_resp):
        assert stt.transcribe(b"RIFF....WAVE") == ""

    # 3. Bridge unreachable -> falls back to local FasterWhisper
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        assert stt.transcribe(b"RIFF....WAVE") == "local whisper fallback"
        fallback.transcribe.assert_called_once()


def test_neural_tts_qwen_primary_falls_back_to_deepgram_on_timeout():
    tts = NeuralTTS(
        quin_url="http://127.0.0.1:18882/v1/audio/speech",
        bridge_url="http://127.0.0.1:8787/v1/audio/speech",
        default_provider="qwen",
        qwen_timeout=0.5,
    )

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        if "18882" in url:
            raise httpx.ReadTimeout("qwen realtime slow")
        resp = MagicMock(status_code=200)
        resp.content = b"\xff\xf3\x60\xc4fake_mp3_audio"
        resp.headers = {"content-type": "audio/mpeg"}
        return resp

    with patch("httpx.Client.post", side_effect=fake_post):
        result = tts.synthesize("Hello Chandler, I am ready.")
        assert result is not None
        assert result.audio_bytes.startswith(b"\xff\xf3")
        assert result.provider == "deepgram"
        assert any("18882" in u for u, _ in calls)
        assert any("8787" in u for u, _ in calls)


def test_voice_conversation_turn_transcribes_converses_and_speaks():
    controller = WrenController(stt_provider="mock", model="auto")
    with (
        patch.object(controller.audio, "start"),
        patch.object(controller.audio, "stop", return_value=b"RIFF....WAVE"),
        patch.object(controller.stt, "transcribe", return_value="What is my next meeting?"),
        patch.object(controller.elevator, "converse") as mock_conv,
        patch.object(controller, "speak_async") as mock_speak,
    ):
        mock_conv.return_value = MagicMock(
            raw_text="What is my next meeting?",
            elevated_text="Your calendar is clear for the next hour.",
            model="auto",
            fallback_used=False,
        )
        controller.start_listening(intent="talk")
        assert controller.state == WrenState.LISTENING
        out = controller.stop_listening()
        assert controller.state == WrenState.IDLE
        assert controller.last_reply == "Your calendar is clear for the next hour."
        assert len(controller.conversation) == 2
        mock_speak.assert_called_once_with("Your calendar is clear for the next hour.")
        assert out is None or getattr(out, "text", "") != ""


def test_silent_dictation_does_not_clobber_existing_draft():
    controller = WrenController(stt_provider="mock", model="auto")
    u = controller.buffer.record_utterance("good draft")
    good = controller.buffer.create_draft_from_utterance(u.id, "Keep this good draft.")
    controller.state_machine.transition(WrenState.DRAFT_READY)

    with (
        patch.object(controller.audio, "start"),
        patch.object(controller.audio, "stop", return_value=b"RIFF....WAVE"),
        patch.object(controller.stt, "transcribe", return_value=""),
    ):
        controller.start_listening(intent="dictate")
        res = controller.stop_listening()
        assert res is None
        assert controller.current_draft.id == good.id
        assert controller.current_draft.text == "Keep this good draft."
        assert controller.state == WrenState.DRAFT_READY
