"""Tests for Wren desktop audio capture and local STT engine."""
from unittest.mock import MagicMock, patch

import pytest

from wren.audio.capture import CaptureState, DesktopAudioCapture
from wren.stt.engine import FasterWhisperSTT, MockSTT


def test_audio_capture_lifecycle_and_state():
    capture = DesktopAudioCapture(sample_rate=16000)
    assert capture.state == CaptureState.IDLE
    
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        
        capture.start()
        assert capture.state == CaptureState.RECORDING
        assert capture.is_recording is True
        
        # Stop capture
        mock_proc.communicate.return_value = (b"\x00" * 32000, b"")
        wav_path = capture.stop()
        assert capture.state == CaptureState.IDLE
        assert capture.is_recording is False
        assert wav_path is not None


def test_cannot_start_recording_twice():
    capture = DesktopAudioCapture()
    with patch("subprocess.Popen"):
        capture.start()
        with pytest.raises(RuntimeError, match="already recording"):
            capture.start()
        capture.stop()


def test_mock_stt_transcription():
    stt = MockSTT(return_text="This is a test transcription.")
    text = stt.transcribe(b"fake_audio_bytes")
    assert text == "This is a test transcription."


def test_faster_whisper_adapter_transcription():
    with patch("faster_whisper.WhisperModel") as mock_model_cls:
        mock_model = MagicMock()
        mock_segment = MagicMock()
        mock_segment.text = "Hello world from faster whisper"
        mock_model.transcribe.return_value = ([mock_segment], None)
        mock_model_cls.return_value = mock_model
        
        stt = FasterWhisperSTT(model_size="tiny.en")
        text = stt.transcribe("/tmp/test.wav")
        assert text == "Hello world from faster whisper"
