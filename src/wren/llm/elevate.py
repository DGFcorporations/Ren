"""Wren LLM prompt cleaner, intent-preserving elevation, and diff engine."""
import difflib
import re
from dataclasses import dataclass

import httpx

SYSTEM_ELEVATE_PROMPT = """You are Wren's Prompt & Text Elevation Engine.
Your task is to polish, clean up, and articulate the user's spoken or raw text into clear, professional text.

HARD LAWS:
1. NEVER alter the core intent, requests, or underlying meaning.
2. NEVER drop or modify specific names, numbers, dates, times, URLs, emails, product names, or code tokens.
3. Eliminate verbal filler ('um', 'uh', 'you know', 'like', stuttered repetitions).
4. Correct spelling, punctuation, capitalization, and grammar.
5. Return ONLY the elevated text. Do NOT add conversational banter, explanations, or quotes.
"""

SYSTEM_REVISE_PROMPT = """You are Wren's Text Revision Engine.
You will receive the CURRENT DRAFT and the USER'S REVISION INSTRUCTION.
Apply the requested changes faithfully while keeping all unaffected facts, names, and numbers intact.
Return ONLY the revised text.
"""


@dataclass
class ElevationResult:
    raw_text: str
    elevated_text: str
    diff: str
    intent_preserved: bool
    model: str
    fallback_used: bool = False


class PromptElevator:
    """Cleans up transcripts and elevates text via local FreeLLMAPI router."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18880/v1",
        model: str = "auto",
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @staticmethod
    def compute_diff(raw: str, elevated: str) -> str:
        """Compute unified line-by-line diff between raw and elevated text."""
        raw_lines = raw.splitlines(keepends=True)
        elev_lines = elevated.splitlines(keepends=True)
        diff = difflib.unified_diff(
            raw_lines,
            elev_lines,
            fromfile="raw_transcript",
            tofile="elevated_draft",
            lineterm="",
        )
        return "\n".join(diff)

    @staticmethod
    def extract_critical_tokens(text: str) -> list[str]:
        """Extract numbers, codes, and capitalized proper nouns to verify preservation."""
        # Numbers and codes: 555-1234, 9872, 3:00, 10am
        numbers = re.findall(r"\b\d+[\d\-\:\.\/]*\b", text)
        # Capitalized words (excluding start of sentences if possible, but keep simple)
        words = re.findall(r"\b[A-Z][a-z0-9_]+\b", text)
        return [t.lower() for t in set(numbers + words)]

    def verify_intent(self, raw: str, elevated: str) -> bool:
        """
        Verify that critical entities (numbers, names) from raw text
        are preserved in the elevated text.
        """
        critical_tokens = self.extract_critical_tokens(raw)
        elevated_lower = elevated.lower()
        for token in critical_tokens:
            if token not in elevated_lower:
                return False
        return True

    def elevate(
        self,
        raw_text: str,
        instruction: str | None = None,
        current_draft_text: str | None = None,
    ) -> ElevationResult:
        """
        Call FreeLLMAPI to clean up and elevate the text.
        Falls back to normalized raw text if LLM is unavailable.
        """
        if current_draft_text and instruction:
            system_prompt = SYSTEM_REVISE_PROMPT
            user_content = f"CURRENT DRAFT:\n{current_draft_text}\n\nINSTRUCTION:\n{instruction}"
        else:
            system_prompt = SYSTEM_ELEVATE_PROMPT
            user_content = f"RAW SPOKEN TEXT:\n{raw_text}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                )
                if res.status_code == 200:
                    data = res.json()
                    elevated_text = data["choices"][0]["message"]["content"].strip()
                    # Strip wrapping quotes if LLM added them
                    if elevated_text.startswith('"') and elevated_text.endswith('"'):
                        elevated_text = elevated_text[1:-1].strip()
                    
                    diff = self.compute_diff(raw_text, elevated_text)
                    intent_ok = self.verify_intent(raw_text, elevated_text)
                    return ElevationResult(
                        raw_text=raw_text,
                        elevated_text=elevated_text,
                        diff=diff,
                        intent_preserved=intent_ok,
                        model=self.model,
                        fallback_used=False,
                    )
        except Exception:
            pass

        # Fallback path if LLM is unavailable
        fallback_text = raw_text.strip()
        if fallback_text and not fallback_text[0].isupper():
            fallback_text = fallback_text[0].upper() + fallback_text[1:]
        if fallback_text and fallback_text[-1] not in ".!?":
            fallback_text += "."

        return ElevationResult(
            raw_text=raw_text,
            elevated_text=fallback_text,
            diff=self.compute_diff(raw_text, fallback_text),
            intent_preserved=True,
            model="local-deterministic-fallback",
            fallback_used=True,
        )
