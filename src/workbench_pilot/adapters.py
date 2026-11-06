"""G1 fixtures are deliberately not live model, speech, or semantic services."""

from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import asdict, dataclass


class LiveIntegrationDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptFixture:
    id: str
    title: str
    transcript: str
    fact_key: str
    draft_prefix: str


FIXTURES = {
    "kickoff": TranscriptFixture(
        "kickoff",
        "Synthetic kickoff reminder",
        "Recall the synthetic Helix kickoff time and prepare a reminder draft.",
        "synthetic.helix.kickoff",
        "SYNTHETIC DRAFT — Helix kickoff: ",
    ),
    "checklist": TranscriptFixture(
        "checklist",
        "Synthetic checklist (multiline)",
        "Recall the synthetic checklist and prepare a multiline draft.",
        "synthetic.helix.checklist",
        "SYNTHETIC CHECKLIST\n",
    ),
    "unicode": TranscriptFixture(
        "unicode",
        "Synthetic Unicode note",
        "Recall the synthetic international note and prepare a draft.",
        "synthetic.helix.note",
        "SYNTHETIC NOTE\n",
    ),
}
SEED_FACTS = {
    "synthetic.helix.kickoff": "14:30 UTC on the synthetic test date",
    "synthetic.helix.checklist": "1. Check the fixture\n2. Review the draft\n3. Verify the local receipt",
    "synthetic.helix.note": "Café — résumé — 東京 — مرحباً — тест ✓",
}


def fixture_catalogue() -> list[dict]:
    return [asdict(f) for f in FIXTURES.values()]


def scripted_transcript(fixture_id: str) -> TranscriptFixture:
    try:
        return FIXTURES[fixture_id]
    except KeyError as exc:
        raise ValueError(
            "Choose a known synthetic fixture; arbitrary speech input is disabled in G1"
        ) from exc


def draft_from_fixture(fixture: TranscriptFixture, fact_value: str) -> str:
    """Literal composition, never executes instructions in memory or page text."""
    return fixture.draft_prefix + fact_value + "\nFixture only. No message has been sent."


def synthetic_tone() -> bytes:
    """A short PCM test tone, not speech synthesis and not an encoded transcript."""
    sample_rate = 16000
    frames = bytearray()
    count = sample_rate // 4
    for i in range(count):
        envelope = min(1.0, i / 160, (count - i) / 160)
        frames.extend(struct.pack("<h", int(3000 * envelope * math.sin(2 * math.pi * 440 * i / sample_rate))))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))
    return output.getvalue()


def require_live_integration(name: str) -> None:
    # No environment variables, credentials, service clients or network calls are read here.
    raise LiveIntegrationDisabled(f"{name} is disabled in G1; explicit G2 authorization is required")


def integration_status() -> dict:
    return {
        "gate": "G1",
        "mode": "offline",
        "authorized_provider_spend_usd": 0,
        "speech_recognition": "UNVERIFIED — scripted transcript fixtures only; microphone disabled",
        "speech_synthesis": "UNVERIFIED — local test tone only; not spoken audio",
        "language_model": "UNVERIFIED — deterministic fixture planner only",
        "pipecat_transport": "UNVERIFIED — not connected or exercised",
        "mem0_semantic_memory": "UNVERIFIED — disabled; SQLite facts/keyword recall only",
        "real_account_browser": "UNVERIFIED — prohibited; owned loopback fixture only",
        "phone_control": "UNVERIFIED — absent",
        "sms": "UNVERIFIED — absent",
        "deployment": "NOT DEPLOYED — local development server only",
    }
