"""Wren desktop microphone capture with in-memory WAV output and live RMS metering."""
import io
import math
import subprocess
import threading
import wave
from array import array
from enum import StrEnum


class CaptureState(StrEnum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    MUTED = "MUTED"
    ERROR = "ERROR"


def compute_pcm16_rms(chunk: bytes) -> float:
    """Return normalized 0.0..1.0 RMS voice energy for 16-bit signed LE PCM."""
    n_bytes = len(chunk) - (len(chunk) % 2)
    if n_bytes <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(chunk[:n_bytes])
    acc = 0.0
    for s in samples:
        acc += float(s) * float(s)
    rms = math.sqrt(acc / len(samples)) / 32768.0
    # Soft gain curve so normal conversational speech (~0.04-0.18 RMS) drives 0.25-1.0 glow
    return min(1.0, rms * 5.5)


class DesktopAudioCapture:
    """
    Push-to-talk microphone capture via parec/arecord.
    Keeps all audio strictly in RAM (zero local disk .wav files per FILE LAW §9)
    and exposes live normalized voice level for real-time orb glow.
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self.state = CaptureState.IDLE
        self.level: float = 0.0
        self.peak_level: float = 0.0
        self._proc: subprocess.Popen | None = None
        self._chunks: list[bytes] = []
        self._reader_thread: threading.Thread | None = None

    @property
    def is_recording(self) -> bool:
        return self.state == CaptureState.RECORDING

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return
        try:
            while True:
                data = stream.read(3200)  # ~100ms at 16kHz s16le mono
                if not isinstance(data, (bytes, bytearray)) or not data:
                    break
                raw = bytes(data)
                self._chunks.append(raw)
                lvl = compute_pcm16_rms(raw)
                self.level = lvl
                if lvl > self.peak_level:
                    self.peak_level = lvl
        except Exception:
            pass
        finally:
            self.level = 0.0

    def start(self) -> None:
        """Start recording raw PCM audio from the default audio input source."""
        if self.state == CaptureState.RECORDING:
            raise RuntimeError("Audio capture is already recording")

        self._chunks = []
        self.level = 0.0
        self.peak_level = 0.0

        cmd = [
            "parec",
            "--format=s16le",
            f"--rate={self.sample_rate}",
            f"--channels={self.channels}",
            "--latency-msec=40",
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            self.state = CaptureState.RECORDING
        except FileNotFoundError:
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

        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(self._proc,), daemon=True
        )
        self._reader_thread.start()

    def stop(self) -> bytes | None:
        """
        Stop recording and return in-memory WAV bytes (zero temp files on disk).
        Returns None if not recording or no audio was captured.
        """
        if not self._proc or self.state != CaptureState.RECORDING:
            self.state = CaptureState.IDLE
            self.level = 0.0
            return None

        proc = self._proc
        reader = self._reader_thread
        self._proc = None
        self._reader_thread = None
        self.state = CaptureState.IDLE

        comm_pcm = b""
        try:
            proc.terminate()
            if not self._chunks:
                out, _ = proc.communicate(timeout=2)
                if isinstance(out, (bytes, bytearray)):
                    comm_pcm = bytes(out)
            else:
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass

        if reader is not None:
            reader.join(timeout=1.0)
        self.level = 0.0

        raw_pcm = b"".join(self._chunks) if self._chunks else comm_pcm
        self._chunks = []
        if not raw_pcm:
            return None

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(raw_pcm)
        return buf.getvalue()

    def cancel(self) -> None:
        """Cancel recording and discard any audio."""
        proc = self._proc
        self._proc = None
        self._reader_thread = None
        self._chunks = []
        self.level = 0.0
        self.state = CaptureState.IDLE
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
