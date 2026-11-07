"""Pluggable Speech-to-Text (STT) interface and local FasterWhisper engine."""
from abc import ABC, abstractmethod


class BaseSTT(ABC):
    """Abstract interface for STT providers."""

    @abstractmethod
    def transcribe(self, audio_source: str | bytes) -> str:
        """Transcribe audio from WAV file path or raw bytes to text."""
        pass


class MockSTT(BaseSTT):
    """Fixture STT for testing and verification."""

    def __init__(self, return_text: str = "Test transcription"):
        self.return_text = return_text

    def transcribe(self, audio_source: str | bytes) -> str:
        return self.return_text


class FasterWhisperSTT(BaseSTT):
    """Local, offline, CPU-optimized STT using faster-whisper with int8 quantization."""

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        return self._model

    def transcribe(self, audio_source: str | bytes) -> str:
        model = self._ensure_model()
        segments, info = model.transcribe(
            audio_source,
            beam_size=5,
            language="en",
            vad_filter=True,
        )
        texts = [segment.text.strip() for segment in segments]
        return " ".join(texts).strip()
