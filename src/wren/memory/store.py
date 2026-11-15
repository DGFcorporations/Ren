"""Ren persistent memory — one SQLite file, three tables, zero daemons.

  facts     — durable knowledge: glossary corrections, contacts, preferences.
              FTS5-indexed for recall; facts tagged 'correction' feed the
              deterministic STT correction layer (misspelling -> replacement).
  agents    — capability registry: which remote agents connected, what they
              can do, when they were last seen. Populated via ren_agent_hello.
  handoffs  — the task queue between Ren and remote agents:
              create -> claim -> complete.

Single file at ~/.config/wren/memory.db (env WREN_MEMORY_DB overrides).
Thread-safe via a write lock; safe for uvicorn worker threads and the MCP
transport's asyncio.to_thread dispatch.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    tags    TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    UNIQUE(key)
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(key, value, tags);
CREATE TABLE IF NOT EXISTS agents (
    name         TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '[]',
    last_seen    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task       TEXT NOT NULL,
    requires   TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'open',  -- open | claimed | done | failed
    created_by TEXT NOT NULL DEFAULT 'ren',
    claimed_by TEXT,
    result     TEXT,
    created    REAL NOT NULL,
    updated    REAL NOT NULL
);
"""


def default_db_path() -> str:
    return os.environ.get("WREN_MEMORY_DB", str(Path.home() / ".config" / "wren" / "memory.db"))


class MemoryStore:
    """Facts + agent registry + handoff queue. All times epoch seconds."""

    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # ---------------- facts ----------------

    def remember(self, key: str, value: str, tags: str = "") -> dict:
        key, value = (key or "").strip(), (value or "").strip()
        if not key or not value:
            raise ValueError("remember requires non-empty key and value")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts(key,value,tags,created,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "tags=excluded.tags, updated=excluded.updated",
                (key.lower(), value, tags, now, now),
            )
            row_id = self._conn.execute(
                "SELECT id FROM facts WHERE key = ?", (key.lower(),)
            ).fetchone()["id"]
            self._conn.execute("DELETE FROM facts_fts WHERE rowid = ?", (row_id,))
            self._conn.execute(
                "INSERT INTO facts_fts(rowid,key,value,tags) VALUES(?,?,?,?)",
                (row_id, key.lower(), value, tags),
            )
            self._conn.commit()
        return {"key": key.lower(), "value": value, "tags": tags}

    def _fts_query(self, query: str) -> str:
        terms = [t for t in re.split(r"[^\w@.\-]+", query) if len(t) > 1]
        return " OR ".join(f'"{t}"' for t in terms) or '""'

    def recall(self, query: str, limit: int = 5) -> list[dict]:
        """FTS over key+value+tags; falls back to substring LIKE on odd input."""
        query = (query or "").strip()
        if not query:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.key, f.value, f.tags, f.updated FROM facts f "
                "JOIN facts_fts ON facts_fts.rowid = f.id "
                "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
                (self._fts_query(query), int(limit)),
            ).fetchall()
            if not rows:  # FTS is strict; LIKE is forgiving
                like = f"%{query[:80]}%"
                rows = self._conn.execute(
                    "SELECT key, value, tags, updated FROM facts "
                    "WHERE key LIKE ? OR value LIKE ? OR tags LIKE ? LIMIT ?",
                    (like, like, like, int(limit)),
                ).fetchall()
        return [dict(r) for r in rows]

    def forget(self, key: str) -> bool:
        key = (key or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM facts WHERE key = ?", (key,)
            ).fetchone()
            cur = self._conn.execute("DELETE FROM facts WHERE key = ?", (key,))
            if row:
                self._conn.execute("DELETE FROM facts_fts WHERE rowid = ?", (row["id"],))
            self._conn.commit()
        return cur.rowcount > 0

    def glossary(self) -> dict[str, str]:
        """Facts tagged 'correction' as {misspelling: replacement} — merged into
        the STT correction table so a spoken 'remember X is spelled Y' becomes a
        permanent dictation fix."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM facts WHERE tags LIKE '%correction%'"
            ).fetchall()
        return {r["key"].removeprefix("spell:"): r["value"] for r in rows}

    def fact_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]

    # ---------------- agents ----------------

    def upsert_agent(self, name: str, capabilities: list[str] | None = None) -> dict:
        name = (name or "").strip().lower()
        if not name:
            raise ValueError("agent name required")
        caps = capabilities or []
        with self._lock:
            self._conn.execute(
                "INSERT INTO agents(name,capabilities,last_seen) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET capabilities=excluded.capabilities, "
                "last_seen=excluded.last_seen",
                (name, json.dumps(caps), time.time()),
            )
            self._conn.commit()
        return {"name": name, "capabilities": caps}

    def agents(self, fresh_s: float | None = None) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, capabilities, last_seen FROM agents ORDER BY last_seen DESC"
            ).fetchall()
        now = time.time()
        out = []
        for r in rows:
            if fresh_s is not None and now - r["last_seen"] > fresh_s:
                continue
            out.append({
                "name": r["name"],
                "capabilities": json.loads(r["capabilities"] or "[]"),
                "age_s": round(now - r["last_seen"], 1),
            })
        return out

    # ---------------- handoffs ----------------

    def create_handoff(
        self, task: str, requires: list[str] | None = None, created_by: str = "ren"
    ) -> dict:
        task = (task or "").strip()
        if not task:
            raise ValueError("handoff requires a task")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO handoffs(task,requires,status,created_by,created,updated) "
                "VALUES(?,?,'open',?,?,?)",
                (task, json.dumps(requires or []), created_by, now, now),
            )
            self._conn.commit()
        return {"id": cur.lastrowid, "task": task, "status": "open"}

    def claim_handoff(self, agent: str, capabilities: list[str] | None = None) -> dict | None:
        """Claim the oldest open handoff whose requires are satisfied by the
        agent's advertised capabilities (empty requires = any agent)."""
        agent = (agent or "").strip().lower()
        caps = set(capabilities or [])
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, task, requires FROM handoffs WHERE status='open' ORDER BY id"
            ).fetchall()
            for r in rows:
                req = set(json.loads(r["requires"] or "[]"))
                if req and not req.issubset(caps):
                    continue
                self._conn.execute(
                    "UPDATE handoffs SET status='claimed', claimed_by=?, updated=? WHERE id=?",
                    (agent, time.time(), r["id"]),
                )
                self._conn.commit()
                return {"id": r["id"], "task": r["task"], "requires": sorted(req)}
        return None

    def complete_handoff(self, handoff_id: int, result: str, ok: bool = True) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE handoffs SET status=?, result=?, updated=? "
                "WHERE id=? AND status='claimed'",
                ("done" if ok else "failed", result or "", time.time(), int(handoff_id)),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"handoff {handoff_id} is not claimed")
        return {"id": int(handoff_id), "status": "done" if ok else "failed"}

    def list_handoffs(self, status: str | None = None, limit: int = 25) -> list[dict]:
        sql = ("SELECT id, task, requires, status, created_by, claimed_by, result, "
               "created, updated FROM handoffs")
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            {**{k: r[k] for k in ("id", "task", "status", "created_by", "claimed_by", "result")},
             "requires": json.loads(r["requires"] or "[]"),
             "age_s": round(time.time() - r["created"], 1)}
            for r in rows
        ]

    def open_handoff_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) c FROM handoffs WHERE status IN ('open','claimed')"
            ).fetchone()["c"]
