"""Delegation layer: persistent memory store, agent registry, handoff queue,
per-agent scoped tokens, voice remember/delegate intents, converse injection."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from wren.core.auth import resolve_agent, tool_allowed
from wren.core.controller import WrenController
from wren.memory.store import MemoryStore
from wren.ui.orb_preview import create_app

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_MEMORY_DB", str(tmp_path / "mem.db"))
    return MemoryStore()


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("WREN_MEMORY_DB", str(tmp_path / "mem.db"))
    return WrenController(stt_provider="mock")


def _initialize(client: TestClient, headers: dict | None = None) -> str:
    res = client.post(
        "/mcp",
        headers=headers or MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}},
    )
    assert res.status_code == 200, res.text
    return res.headers["mcp-session-id"]


def _tools_call(client, session_id, msg_id, name, arguments, headers=None):
    res = client.post(
        "/mcp",
        headers={**(headers or MCP_HEADERS), "mcp-session-id": session_id},
        json={"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )
    assert res.status_code == 200, res.text
    return res.json()["result"]


# ---------------- MemoryStore ----------------

def test_facts_remember_recall_forget(store):
    store.remember("dana email", "dana@dgfcorp.com", tags="contact")
    store.remember("deploy branch", "pilot/voice-memory-browser")
    hits = store.recall("dana")
    assert hits and hits[0]["value"] == "dana@dgfcorp.com"
    assert store.recall("nonexistent gibberish") == []
    assert store.forget("dana email") is True
    assert store.recall("dana") == []
    assert store.forget("dana email") is False


def test_facts_upsert_updates(store):
    store.remember("k", "v1")
    store.remember("k", "v2")
    hits = store.recall("k")
    assert len(hits) == 1 and hits[0]["value"] == "v2"


def test_glossary_facts_feed_corrections(store):
    store.remember("spell:deering", "Dearing", tags="correction")
    assert store.glossary() == {"deering": "Dearing"}


# ---------------- agents registry + handoffs ----------------

def test_agent_registry_and_freshness(store):
    store.upsert_agent("bart", ["browser", "files"])
    online = store.agents(fresh_s=600)
    assert online[0]["name"] == "bart"
    assert online[0]["capabilities"] == ["browser", "files"]
    assert store.agents(fresh_s=0) == []


def test_handoff_lifecycle_capability_match(store):
    store.create_handoff("check the tunnel", requires=["shell"])
    # Agent without 'shell' can't claim it.
    assert store.claim_handoff("q", ["files"]) is None
    # Agent with 'shell' can.
    claimed = store.claim_handoff("bart", ["shell", "files"])
    assert claimed["task"] == "check the tunnel"
    assert store.complete_handoff(claimed["id"], "tunnel healthy")["status"] == "done"
    hist = store.list_handoffs()
    assert hist[0]["status"] == "done" and hist[0]["result"] == "tunnel healthy"


def test_handoff_no_requires_claimable_by_anyone(store):
    store.create_handoff("summarize the log")
    assert store.claim_handoff("q", [])["task"] == "summarize the log"
    with pytest.raises(ValueError):
        store.complete_handoff(999, "x")  # not claimed


# ---------------- controller intents ----------------

def test_voice_remember_and_spelling_glossary(controller):
    reply = controller.remember("dana's email is dana@dgfcorp.com")
    assert "Remembered" in reply
    assert controller.memory.recall("dana email")[0]["value"] == "dana@dgfcorp.com"

    reply = controller.remember("deering is spelled D-E-E-R-I-N-G")
    assert "Deering" in reply
    assert controller.memory.glossary()["deering"] == "Deering"


def test_voice_delegate_queues_handoff(controller):
    reply = controller.delegate("bart", "check the tunnel")
    assert "not connected" in reply  # honest: bart hasn't said hello
    assert controller.memory.list_handoffs("open")[0]["task"] == "check the tunnel"

    controller.memory.upsert_agent("bart", ["shell"])
    reply = controller.delegate("bart", "check it again")
    assert "picks it up" in reply


def test_converse_intents_short_circuit_llm(controller, monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(controller.elevator, "converse", spy)
    reply = controller.converse("remember that the deploy key is in the vault", source="voice")
    assert "Remembered" in reply
    spy.assert_not_called()
    reply = controller.converse("ask bart to run the checks", source="voice")
    assert "Queued for bart" in reply
    spy.assert_not_called()
    assert controller.memory.list_handoffs("open")


def test_converse_injects_memory_and_agents(controller, monkeypatch):
    controller.memory.remember("project codename", "Plasma Orb")
    controller.memory.upsert_agent("bart", ["browser"])
    captured = {}

    def fake_converse(text, screen_note="", history=None):
        captured["note"] = screen_note
        r = MagicMock()
        r.elevated_text = "ok"
        return r

    monkeypatch.setattr(controller.elevator, "converse", fake_converse)
    controller.converse("what is the project codename?", source="voice")
    assert "MEMORY" in captured["note"]
    assert "Plasma Orb" in captured["note"]
    assert "bart" in captured["note"]


def test_brainstorm_path_has_no_memory_injection(controller, monkeypatch):
    """Elevate (brainstorm/dictate) never sees MEMORY context — unanchored."""
    controller.memory.remember("project codename", "Plasma Orb")
    spy = MagicMock(return_value=MagicMock(elevated_text="out", diff="", intent_preserved=True, model="m"))
    monkeypatch.setattr(controller.elevator, "elevate", spy)
    controller.stt = type("S", (), {"transcribe": lambda self, b: "brainstorm the codename"})()
    controller.audio = MagicMock()
    controller.audio.stop.return_value = b"RIFF" + b"\x00" * 100
    controller.stop_listening(intent="dictate")
    args, kwargs = spy.call_args
    sent = args[0] if args else kwargs.get("prompt_text", "")
    assert "MEMORY" not in sent and "Plasma Orb" not in sent


# ---------------- MCP tools end-to-end ----------------

def test_mcp_memory_handoff_speak_tools(controller):
    with TestClient(create_app(controller)) as client:
        sid = _initialize(client)
        r = _tools_call(client, sid, 2, "ren_agent_hello",
                        {"name": "bart", "capabilities": ["shell"]})
        assert r["isError"] is False
        assert "bart" in controller.memory.agents(fresh_s=600)[0]["name"]

        r = _tools_call(client, sid, 3, "ren_memory_remember",
                        {"key": "tunnel", "value": "ren.corporations.group"})
        assert r["isError"] is False
        r = _tools_call(client, sid, 4, "ren_memory_recall", {"query": "tunnel"})
        assert "ren.corporations.group" in r["content"][0]["text"]

        r = _tools_call(client, sid, 5, "ren_handoff_create",
                        {"task": "ssd check", "requires": ["shell"]})
        hid = json.loads(r["content"][0]["text"])["id"]
        r = _tools_call(client, sid, 6, "ren_handoff_claim",
                        {"agent": "bart", "capabilities": ["shell"]})
        assert json.loads(r["content"][0]["text"])["id"] == hid
        r = _tools_call(client, sid, 7, "ren_handoff_complete",
                        {"id": hid, "result": "all clear"})
        assert json.loads(r["content"][0]["text"])["status"] == "done"


def test_mcp_speak_calls_tts(controller, monkeypatch):
    spoken = []
    monkeypatch.setattr(controller, "speak_async", lambda t, provider=None: spoken.append(t))
    with TestClient(create_app(controller)) as client:
        sid = _initialize(client)
        r = _tools_call(client, sid, 2, "ren_speak", {"text": "bart here — done"})
        assert r["isError"] is False
        assert spoken == ["bart here — done"]


# ---------------- per-agent scoped tokens ----------------

def test_resolve_agent_and_scopes(tmp_path, monkeypatch):
    tok_file = tmp_path / "agent-tokens.json"
    tok_file.write_text(json.dumps({
        "btok": {"name": "bart", "scopes": ["*"]},
        "qtok": {"name": "q", "scopes": ["read"]},
    }))
    monkeypatch.setenv("WREN_AGENT_TOKENS", str(tok_file))
    assert resolve_agent("btok", "master") == {"name": "bart", "scopes": ["*"]}
    assert resolve_agent("qtok", "master")["name"] == "q"
    assert resolve_agent("master", "master")["scopes"] == ["*"]
    assert resolve_agent("nope", "master") is None

    read_q = {"name": "q", "scopes": ["read"]}
    assert tool_allowed(read_q, "ren_status")
    assert tool_allowed(read_q, "ren_memory_recall")
    assert tool_allowed(read_q, "ren_files", {"op": "read"})
    assert not tool_allowed(read_q, "ren_files", {"op": "write"})
    assert not tool_allowed(read_q, "ren_dictate")
    assert not tool_allowed(read_q, "ren_speak")
    assert tool_allowed({"name": "b", "scopes": ["*"]}, "ren_speak")


def test_scoped_agent_token_end_to_end(tmp_path, monkeypatch, controller):
    """A 'read' agent token: read tools pass, mutating tools and REST 403."""
    tok_file = tmp_path / "agent-tokens.json"
    tok_file.write_text(json.dumps({"qtok": {"name": "q", "scopes": ["read"]}}))
    monkeypatch.setenv("WREN_AGENT_TOKENS", str(tok_file))
    # cf-connecting-ip forces the non-loopback auth path in the middleware.
    q_headers = {**MCP_HEADERS, "cf-connecting-ip": "203.0.113.9",
                 "authorization": "Bearer qtok"}
    with TestClient(create_app(controller)) as client:
        sid = _initialize(client, headers=q_headers)
        r = _tools_call(client, sid, 2, "ren_status", {}, headers=q_headers)
        assert r["isError"] is False
        r = _tools_call(client, sid, 3, "ren_memory_remember",
                        {"key": "k", "value": "v"}, headers=q_headers)
        assert r["isError"] is True
        assert "scope denied" in r["content"][0]["text"]
        # REST mutations blocked for read-scoped agents.
        res = client.post("/api/dictate", json={"text": "x"},
                          headers={"cf-connecting-ip": "203.0.113.9",
                                   "authorization": "Bearer qtok"})
        assert res.status_code == 403
        # Reads still fine.
        res = client.get("/api/memory/recall?query=x",
                         headers={"cf-connecting-ip": "203.0.113.9",
                                  "authorization": "Bearer qtok"})
        assert res.status_code == 200


def test_status_exposes_delegation_counts(controller):
    controller.memory.remember("k", "v")
    controller.memory.create_handoff("t")
    controller.memory.upsert_agent("bart", [])
    status = controller.status_dict(True)
    assert status["memory_facts"] == 1
    assert status["handoffs_open"] == 1
    assert status["agents_online"] == 1
