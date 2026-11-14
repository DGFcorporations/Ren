"""Pluggable Speech-to-Text (STT) interface: Cloud AST (Groq/Deepgram) + FasterWhisper fallback."""
import base64
import io
import os
from abc import ABC, abstractmethod
from pathlib import Path

import httpx


class BaseSTT(ABC):
    """Abstract interface for STT providers."""

    @abstractmethod
    def transcribe(self, audio_source: str | bytes) -> str:
        """Transcribe audio from WAV file path or in-memory WAV bytes to text."""
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
        src = (
            io.BytesIO(bytes(audio_source))
            if isinstance(audio_source, (bytes, bytearray))
            else audio_source
        )
        segments, _ = model.transcribe(
            src,
            beam_size=5,
            language="en",
            vad_filter=True,
        )
        texts = [segment.text.strip() for segment in segments]
        return " ".join(texts).strip()


class CloudHybridSTT(BaseSTT):
    """
    Fast cloud/daemon AST chain (~0.8s) with offline FasterWhisper fallback:
      1. Workstation Voice Bridge (:8787 /v1/audio/transcriptions -> VoiceStudio / Groq Whisper / Deepgram)
      2. Direct Deepgram Nova-2 if DEEPGRAM_API_KEY is in env
      3. Local FasterWhisperSTT fallback
    """

    BRIDGE_SILENCE_SENTINELS = {"voice command captured", "no audio detected"}

    def __init__(
        self,
        bridge_url: str | None = None,
        fallback: BaseSTT | None = None,
        timeout: float = 8.0,
    ):
        base = (bridge_url or os.environ.get("WREN_VOICE_BRIDGE_HOST", "http://127.0.0.1:8787")).rstrip("/")
        self.transcribe_url = (
            base if base.endswith("/transcriptions") else f"{base}/v1/audio/transcriptions"
        )
        self.fallback = fallback or FasterWhisperSTT(model_size="base.en")
        self.timeout = timeout
        self.last_provider: str = ""

    @staticmethod
    def _to_bytes(audio_source: str | bytes) -> bytes:
        if isinstance(audio_source, (bytes, bytearray)):
            return bytes(audio_source)
        return Path(audio_source).read_bytes()

    def transcribe(self, audio_source: str | bytes) -> str:
        raw_bytes = self._to_bytes(audio_source)
        if not raw_bytes:
            return ""

        # 1. Workstation Bridge (:8787) — VoiceStudio / Groq Whisper AST / Deepgram Nova-2
        try:
            payload = {
                "audioBase64": base64.b64encode(raw_bytes).decode("ascii"),
                "mimeType": "audio/wav",
            }
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(self.transcribe_url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    text = (data.get("text") or "").strip()
                    self.last_provider = data.get("provider", "workstation-bridge")
                    if text.lower() in self.BRIDGE_SILENCE_SENTINELS:
                        return ""
                    return text
        except Exception:
            pass

        # 2. Direct Deepgram Nova-2 when key is available in env
        dg_key = os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM_API")
        if dg_key:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    res = client.post(
                        "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true",
                        content=raw_bytes,
                        headers={
                            "Authorization": f"Token {dg_key}",
                            "Content-Type": "audio/wav",
                        },
                    )
                    if res.status_code == 200:
                        channels = res.json().get("results", {}).get("channels", [])
                        if channels and channels[0].get("alternatives"):
                            self.last_provider = "deepgram-direct"
                            return (channels[0]["alternatives"][0].get("transcript") or "").strip()
            except Exception:
                pass

        # 3. Local FasterWhisper fallback
        self.last_provider = "faster-whisper"
        return self.fallback.transcribe(audio_source)
