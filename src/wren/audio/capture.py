"""Wren desktop microphone capture with explicit push-to-talk gating."""
import os
import subprocess
import tempfile
import wave
from enum import StrEnum


class CaptureState(StrEnum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    MUTED = "MUTED"
    ERROR = "ERROR"


class DesktopAudioCapture:
    """
    Manages push-to-talk microphone recording via parec/pw-record/arecord.
    Mic is strictly inactive unless explicitly started by user action.
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self.state = CaptureState.IDLE
        self._proc: subprocess.Popen | None = None
        self._temp_wav: str | None = None

    @property
    def is_recording(self) -> bool:
        return self.state == CaptureState.RECORDING

    def start(self) -> None:
        """Start recording raw PCM audio from the default audio input source."""
        if self.state == CaptureState.RECORDING:
            raise RuntimeError("Audio capture is already recording")

        # Prefer parec (PulseAudio/PipeWire native), fallback to arecord
        cmd = [
            "parec",
            "--format=s16le",
            f"--rate={self.sample_rate}",
            f"--channels={self.channels}",
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.state = CaptureState.RECORDING
        except FileNotFoundError:
            # Fallback to arecord
            fallback_cmd = [
                "arecord",
                "-f", "S16_LE",
                "-r", str(self.sample_rate),
                "-c", str(self.channels),
                "-t", "raw",
            ]
            try:
                self._proc = subprocess.Popen(
                    fallback_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self.state = CaptureState.RECORDING
            except Exception as e:
                self.state = CaptureState.ERROR
                raise RuntimeError(f"Failed to start audio recording: {e}") from e

    def stop(self) -> str | None:
        """
        Stop recording, write captured PCM bytes into a temporary WAV file,
        and return the file path. Returns None if not recording.
        """
        if not self._proc or self.state != CaptureState.RECORDING:
            self.state = CaptureState.IDLE
            return None

        proc = self._proc
        self._proc = None
        self.state = CaptureState.IDLE

        try:
            proc.terminate()
            raw_pcm, _ = proc.communicate(timeout=2)
        except Exception:
            try:
                proc.kill()
                raw_pcm, _ = proc.communicate(timeout=1)
            except Exception:
                raw_pcm = b""

        if not raw_pcm:
            return None

        # Write to temporary WAV
        fd, temp_path = tempfile.mkstemp(prefix="wren_mic_", suffix=".wav")
        os.close(fd)
        with wave.open(temp_path, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(raw_pcm)

        self._temp_wav = temp_path
        return temp_path

    def cancel(self) -> None:
        """Cancel recording and discard any audio."""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.communicate(timeout=1)
            except Exception:
                pass
            self._proc = None
        self.state = CaptureState.IDLE
