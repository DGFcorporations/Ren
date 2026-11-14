"""Neural Text-to-Speech engine: Qwen primary -> Deepgram Aura fallback -> VoiceStudio -> espeak-ng.

Honors FILE LAW §9: zero audio files are ever written to local disk or /tmp.
Synthesized audio stays in RAM; archival goes exclusively to Google Drive when mounted
(Workstation Bridge :8787 already archives its own outputs to Drive).
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

DASHSCOPE_MMGEN_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEEPGRAM_SPEAK_URL = "https://api.deepgram.com/v1/speak?model=aura-orion-en"
DRIVE_MOUNT = Path("/home/chandler/Dev/g/My Drive")
DRIVE_VOICE_DIR = DRIVE_MOUNT / "Voice_Clones" / "Ren_Voices"


@dataclass
class TTSAudio:
    audio_bytes: bytes
    content_type: str
    provider: str


def clean_spoken_text(text: str) -> str:
    """Convert LLM/markdown text into clean natural speech without syntax noise."""
    s = text or ""
    s = re.sub(r"```[\s\S]*?```", " ", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[*_#~>|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:1200]


def _archive_to_drive_async(audio_bytes: bytes, provider: str) -> None:
    """Archive directly-synthesized audio to Google Drive when mounted (never local disk)."""
    def _worker() -> None:
        try:
            if DRIVE_MOUNT.is_dir():
                DRIVE_VOICE_DIR.mkdir(parents=True, exist_ok=True)
                ext = "wav" if audio_bytes.startswith(b"RIFF") else "mp3"
                dest = DRIVE_VOICE_DIR / f"ren-{provider}-{int(time.time() * 1000)}.{ext}"
                dest.write_bytes(audio_bytes)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _detect_mime(audio_bytes: bytes, header_ct: str = "") -> str:
    if audio_bytes.startswith(b"RIFF"):
        return "audio/wav"
    if audio_bytes.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return "audio/mpeg"
    if "mpeg" in header_ct or "mp3" in header_ct:
        return "audio/mpeg"
    return "audio/wav"


class NeuralTTS:
    """Multi-provider neural speech engine with Qwen primary + Deepgram 0.6s fallback."""

    VALID_PROVIDERS = {"qwen", "deepgram", "voicestudio", "auto"}

    def __init__(
        self,
        quin_url: str | None = None,
        bridge_url: str | None = None,
        voicestudio_url: str | None = None,
        default_provider: str = "qwen",
        qwen_timeout: float = 1.5,
    ):
        self.quin_url = quin_url or os.environ.get(
            "QUIN_TTS_URL", "http://127.0.0.1:18882/v1/audio/speech"
        )
        self.bridge_url = bridge_url or os.environ.get(
            "WREN_VOICE_BRIDGE_URL", "http://127.0.0.1:8787/v1/audio/speech"
        )
        self.voicestudio_url = voicestudio_url or os.environ.get(
            "VOICE_STUDIO_URL", "http://127.0.0.1:3900/v1/audio/speech"
        )
        self.provider = default_provider if default_provider in self.VALID_PROVIDERS else "qwen"
        self.qwen_timeout = qwen_timeout
        self.muted: bool = False
        self._play_lock = threading.Lock()
        self._play_proc: subprocess.Popen | None = None
        self.last_provider_used: str = ""

    def set_provider(self, provider: str) -> str:
        p = provider.strip().lower()
        if p not in self.VALID_PROVIDERS:
            raise ValueError(f"Unsupported TTS provider '{provider}'. Valid: {self.VALID_PROVIDERS}")
        self.provider = p
        return self.provider

    def _try_qwen_direct(self, spoken: str) -> TTSAudio | None:
        api_key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QUIN_API_KEY")
        if not api_key:
            return None
        try:
            with httpx.Client(timeout=self.qwen_timeout) as client:
                r = client.post(
                    DASHSCOPE_MMGEN_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": os.environ.get("DASHSCOPE_MODEL", "qwen3-tts-flash"),
                        "input": {
                            "text": spoken,
                            "voice": os.environ.get("DASHSCOPE_VOICE", "Cherry"),
                        },
                    },
                )
                if r.status_code != 200:
                    return None
                url = ((r.json().get("output") or {}).get("audio") or {}).get("url")
                if not url:
                    return None
                audio_res = client.get(url)
                if audio_res.status_code == 200 and audio_res.content:
                    _archive_to_drive_async(audio_res.content, "qwen")
                    ct = _detect_mime(audio_res.content, audio_res.headers.get("content-type", ""))
                    return TTSAudio(audio_res.content, ct, "qwen")
        except Exception:
            pass
        return None

    def _try_quin_gateway(self, spoken: str) -> TTSAudio | None:
        try:
            with httpx.Client(timeout=self.qwen_timeout) as client:
                r = client.post(
                    self.quin_url,
                    json={
                        "input": spoken,
                        "voice": "longanqian",
                        "model": "qwen-audio-3.0-realtime-plus",
                    },
                )
                if r.status_code == 200 and r.content:
                    _archive_to_drive_async(r.content, "qwen")
                    ct = _detect_mime(r.content, r.headers.get("content-type", ""))
                    return TTSAudio(r.content, ct, "qwen")
        except Exception:
            pass
        return None

    def _try_deepgram(self, spoken: str) -> TTSAudio | None:
        # 1. Workstation Bridge :8787 (already holds vault DEEPGRAM_KEY and archives to Drive)
        try:
            with httpx.Client(timeout=8.0) as client:
                r = client.post(
                    self.bridge_url,
                    json={"input": spoken, "voice": "ace", "provider": "auto"},
                )
                if r.status_code == 200 and r.content:
                    ct = _detect_mime(r.content, r.headers.get("content-type", ""))
                    return TTSAudio(r.content, ct, "deepgram")
        except Exception:
            pass

        # 2. Direct Deepgram Aura API when key is in environment
        dg_key = os.environ.get("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM_API")
        if dg_key:
            try:
                with httpx.Client(timeout=8.0) as client:
                    r = client.post(
                        DEEPGRAM_SPEAK_URL,
                        headers={"Authorization": f"Token {dg_key}"},
                        json={"text": spoken},
                    )
                    if r.status_code == 200 and r.content:
                        _archive_to_drive_async(r.content, "deepgram")
                        return TTSAudio(r.content, "audio/mpeg", "deepgram")
            except Exception:
                pass
        return None

    def _try_voicestudio(self, spoken: str) -> TTSAudio | None:
        try:
            with httpx.Client(timeout=8.0) as client:
                r = client.post(
                    self.voicestudio_url,
                    json={"input": spoken, "voice": "echo", "model": "tts-1"},
                )
                if r.status_code == 200 and r.content:
                    _archive_to_drive_async(r.content, "voicestudio")
                    ct = _detect_mime(r.content, r.headers.get("content-type", ""))
                    return TTSAudio(r.content, ct, "voicestudio")
        except Exception:
            pass
        return None

    def _try_espeak_ram(self, spoken: str) -> TTSAudio | None:
        try:
            res = subprocess.run(
                ["espeak-ng", "-v", "en", "-s", "175", "--stdout", spoken],
                capture_output=True,
                timeout=6,
                check=False,
            )
            if res.returncode == 0 and res.stdout:
                return TTSAudio(res.stdout, "audio/wav", "espeak-ng")
        except Exception:
            pass
        return None

    def synthesize(self, text: str, provider: str | None = None) -> TTSAudio | None:
        """Synthesize spoken audio in RAM using the configured provider fallback chain."""
        spoken = clean_spoken_text(text)
        if not spoken:
            return None

        pref = (provider or self.provider or "qwen").strip().lower()
        if pref == "deepgram":
            chain = [self._try_deepgram, self._try_qwen_direct, self._try_voicestudio]
        elif pref == "voicestudio":
            chain = [self._try_voicestudio, self._try_deepgram, self._try_qwen_direct]
        else:
            # Default: Qwen primary -> Deepgram Aura 0.6s fallback -> VoiceStudio
            chain = [
                self._try_qwen_direct,
                self._try_quin_gateway,
                self._try_deepgram,
                self._try_voicestudio,
            ]

        for step in chain:
            out = step(spoken)
            if out is not None:
                self.last_provider_used = out.provider
                return out

        fallback = self._try_espeak_ram(spoken)
        if fallback is not None:
            self.last_provider_used = fallback.provider
        return fallback

    def stop(self) -> None:
        """Immediately halt any active speech playback (barge-in support)."""
        with self._play_lock:
            proc = self._play_proc
            self._play_proc = None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=0.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def play_audio_bytes(
        self,
        audio: TTSAudio,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        """Play in-memory MP3/WAV bytes through system speakers without temp files."""
        if self.muted or not audio.audio_bytes:
            return
        self.stop()

        cmds = [["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"]]
        if audio.content_type == "audio/wav":
            cmds.append(["aplay", "-q", "-"])

        proc: subprocess.Popen | None = None
        for cmd in cmds:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                break
            except FileNotFoundError:
                continue

        if proc is None:
            return

        with self._play_lock:
            self._play_proc = proc

        def _feed() -> None:
            try:
                if proc.stdin:
                    proc.stdin.write(audio.audio_bytes)
                    proc.stdin.close()
            except Exception:
                pass

        threading.Thread(target=_feed, daemon=True).start()

        t0 = time.monotonic()
        try:
            while proc.poll() is None:
                if on_level is not None:
                    t = time.monotonic() - t0
                    pulse = 0.35 + 0.55 * abs(math.sin(t * 9.5)) * (0.65 + 0.35 * math.sin(t * 3.7))
                    on_level(min(1.0, pulse))
                time.sleep(0.05)
        finally:
            if on_level is not None:
                on_level(0.0)
            with self._play_lock:
                if self._play_proc is proc:
                    self._play_proc = None
