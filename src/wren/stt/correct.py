"""Post-transcription term corrections applied to raw STT text.

Whisper/Deepgram routinely misspell product and person names — the canonical
case being the assistant's own name, which comes back as "Rin" or "Wren".
A deterministic whole-word rewrite fixes the misspelling instantly on every
provider, before the transcript can reach a draft, the conversation, or an
elevated prompt.

Override/extend the table with WREN_STT_CORRECTIONS, e.g.:
    WREN_STT_CORRECTIONS="devon:Devin,brock:Brock"
"""
from __future__ import annotations

import os
import re

_DEFAULTS: dict[str, str] = {
    "rin": "Ren",
    "rinn": "Ren",
    "renn": "Ren",
    "ryn": "Ren",
    "wren": "Ren",
}


def load_corrections() -> dict[str, str]:
    """Correction table: built-ins plus WREN_STT_CORRECTIONS 'from:to' pairs."""
    table = dict(_DEFAULTS)
    raw = os.environ.get("WREN_STT_CORRECTIONS", "").strip()
    if raw:
        for pair in raw.split(","):
            if ":" in pair:
                src, dst = pair.split(":", 1)
                if src.strip():
                    table[src.strip().lower()] = dst.strip()
    return table


def apply_corrections(text: str, table: dict[str, str] | None = None) -> str:
    """Whole-word replace misspelled terms. A match followed by '.' is left
    alone so dotted code tokens (wren.service, wren.cli) are not corrupted."""
    if not text:
        return text
    table = load_corrections() if table is None else table
    if not table:
        return text
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in sorted(table, key=len, reverse=True)) + r")\b(?!\.)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: table[m.group(1).lower()], text)
