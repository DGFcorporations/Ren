"""Per-agent bearer tokens for Ren's remote surfaces.

The master token (auth-token file / WREN_AUTH_TOKEN) keeps full access and
resolves as agent "master". Additional agents get their own tokens in
~/.config/wren/agent-tokens.json (env WREN_AGENT_TOKENS overrides):

    {
      "tok_aaa": {"name": "bart", "scopes": ["*"]},
      "tok_bbb": {"name": "q",    "scopes": ["read"]}
    }

Scopes gate MCP tool classes: "read" allows read-only tools (status, screen,
files list/read, memory recall, handoff list); every other tool requires "*"
or "write". Unknown tokens resolve to None (rejected upstream as 401).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Tool names that mutate workstation state or speak — anything NOT in this set
# is read-only and allowed for "read"-scoped agents.
WRITE_TOOLS = {
    "ren_converse", "ren_dictate", "ren_paste_commit", "ren_browser",
    "ren_files", "ren_set_mode", "ren_memory_remember", "ren_memory_forget",
    "ren_handoff_create", "ren_handoff_claim", "ren_handoff_complete",
    "ren_speak",
}
# ren_files is dual-mode; op=write is checked separately in dispatch.
READ_FILE_OPS = {"list", "read"}


def _tokens_path() -> Path:
    return Path(
        os.environ.get(
            "WREN_AGENT_TOKENS", str(Path.home() / ".config" / "wren" / "agent-tokens.json")
        )
    )


def load_agent_tokens() -> dict:
    try:
        data = json.loads(_tokens_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_agent(token: str | None, master_token: str | None) -> dict | None:
    """Map a bearer token to an agent identity {'name','scopes'} or None."""
    if not token:
        return None
    import secrets

    if master_token and secrets.compare_digest(token, master_token):
        return {"name": "master", "scopes": ["*"]}
    for tok, meta in load_agent_tokens().items():
        if secrets.compare_digest(token, str(tok)):
            name = str(meta.get("name") or "agent").lower()
            scopes = meta.get("scopes") or ["*"]
            return {"name": name, "scopes": list(scopes)}
    return None


def tool_allowed(agent: dict | None, tool: str, args: dict | None = None) -> bool:
    """Scope check: '*' or 'write' agents can call anything; 'read' agents are
    limited to read-only tools (and read-only ren_files ops)."""
    if agent is None:
        return False
    scopes = set(agent.get("scopes") or [])
    if "*" in scopes or "write" in scopes:
        return True
    if "read" not in scopes:
        return False
    if tool == "ren_files":
        return (args or {}).get("op") in READ_FILE_OPS
    return tool not in WRITE_TOOLS
