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


def test_audio_capture_returns_in_memory_wav_bytes_and_tracks_rms_level():
    """Capture must keep audio in RAM (bytes, zero /tmp/*.wav files) and compute live RMS level."""
    import io
    import struct

    capture = DesktopAudioCapture(sample_rate=16000)
    # 0.1s of loud 16-bit PCM tone
    loud_pcm = struct.pack("<1600h", *([12000, -12000] * 800))

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdout = io.BytesIO(loud_pcm)
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        capture.start()
        wav_data = capture.stop()
        assert isinstance(wav_data, bytes)
        assert wav_data.startswith(b"RIFF")
        assert capture.peak_level > 0.2
        assert capture.level == 0.0  # resets to 0 once stopped


def test_faster_whisper_accepts_in_memory_wav_bytes():
    import io

    with patch("faster_whisper.WhisperModel") as mock_model_cls:
        mock_model = MagicMock()
        seg = MagicMock()
        seg.text = "in memory works"
        mock_model.transcribe.return_value = ([seg], None)
        mock_model_cls.return_value = mock_model

        stt = FasterWhisperSTT(model_size="tiny.en")
        out = stt.transcribe(b"RIFF....WAVE")
        assert out == "in memory works"
        passed_arg = mock_model.transcribe.call_args.args[0]
        assert isinstance(passed_arg, io.BytesIO)
