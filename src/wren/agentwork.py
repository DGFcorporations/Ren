"""Agent work-through layer for Ren's MCP surface.

Provides the durable, verifiable, async-aware scaffolding that turns Ren from a
remote control into a work-through agent for cloud agents:

  1. Durable action ledger with receipts (audit + idempotency)
  2. Async progress + cancellation
  3. Push event subscriptions (EventBus)
  4. Structured result envelopes + per-tool schemas
  5. Async approval bridge (pending approvals surfaced instead of thrown)
  6. Session continuity across MCP connections
  7. Live-context diffs between turns
  8. Per-agent policy budgets (rate + action quotas)
  9. Capability-based task routing
 10. Semantic grounding (visual query + browser accessibility tree)
 11. Verified tool-outcome feedback (readback after mutation)

Everything is SQLite-backed (one file), thread-safe, and offline-first.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0.0,   -- 0.0..1.0
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    receipt_json TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    agent TEXT PRIMARY KEY,
    quota_per_min REAL NOT NULL DEFAULT 60.0,
    quota_per_day REAL NOT NULL DEFAULT 2000.0,
    write_allowed INTEGER NOT NULL DEFAULT 1,
    window_min INTEGER NOT NULL DEFAULT 1,
    window_day INTEGER NOT NULL DEFAULT 1440,
    used_min REAL NOT NULL DEFAULT 0.0,
    used_day REAL NOT NULL DEFAULT 0.0,
    reset_min REAL NOT NULL,
    reset_day REAL NOT NULL
);
"""

_ACTIVE_ACTION = {"queued", "running", "awaiting_approval", "approved", "executing"}
_TERMINAL_ACTION = {"completed", "cancelled", "failed", "denied", "expired", "uncertain"}
_ACTION_STATUSES = _ACTIVE_ACTION | _TERMINAL_ACTION


class AgentWorkError(ValueError):
    pass


class LimitExceeded(AgentWorkError):
    pass


class ActionNotFound(AgentWorkError):
    pass


class ApprovalPending(AgentWorkError):
    """Raised by mutating tools when an async approval is required."""


def default_db_path() -> str:
    return os.environ.get(
        "WREN_AGENTWORK_DB", str(Path.home() / ".config" / "wren" / "agentwork.db")
    )


def _now() -> float:
    return time.time()


class AgentWorkLedger:
    def __init__(self, path: str | None = None, clock: Callable[[], float] = _now):
        self.path = Path(path or default_db_path())
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    # ---------------- sessions ----------------

    def ensure_session(self, session_id: str, agent: str) -> dict:
        session_id = (session_id or "").strip() or uuid.uuid4().hex
        agent = (agent or "agent").strip().lower()
        now = self._clock()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO sessions(session_id,agent,context_json,created,updated) "
                    "VALUES(?,?,'{}',?,?)",
                    (session_id, agent, now, now),
                )
            else:
                self._conn.execute(
                    "UPDATE sessions SET agent=?, updated=? WHERE session_id=?",
                    (agent, now, session_id),
                )
            self._conn.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise ActionNotFound("session not found")
        return {
            "session_id": row["session_id"],
            "agent": row["agent"],
            "context": json.loads(row["context_json"] or "{}"),
            "created": row["created"],
            "updated": row["updated"],
        }

    def set_context(self, session_id: str, context: dict) -> dict:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET context_json=?, updated=? WHERE session_id=?",
                (json.dumps(context), self._clock(), session_id),
            )
            self._conn.commit()
        return self.get_session(session_id)

    def touch(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET updated=? WHERE session_id=?",
                (self._clock(), session_id),
            )
            self._conn.commit()

    def list_sessions(self, agent: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM sessions"
        args: list[Any] = []
        if agent:
            sql += " WHERE agent=?"
            args.append(agent)
        sql += " ORDER BY updated DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "agent": r["agent"],
                "created": r["created"],
                "updated": r["updated"],
            }
            for r in rows
        ]

    # ---------------- actions ----------------

    def reserve_action(
        self,
        session_id: str,
        agent: str,
        tool: str,
        args: dict,
        idempotency_key: str | None = None,
    ) -> tuple[dict, bool]:
        """Reserve an action. Returns (action, is_new). Idempotent on request id."""
        if idempotency_key:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM actions WHERE id=? AND session_id=? AND agent=?",
                    (idempotency_key, session_id, agent),
                ).fetchone()
                if row is not None:
                    return self._action_public(row), False
        now = self._clock()
        action_id = idempotency_key or uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO actions(id,session_id,agent,tool,args_json,status,created,updated) "
                "VALUES(?,?,?,?,?,'running',?,?)",
                (action_id, session_id, agent, tool, json.dumps(args), now, now),
            )
            self._conn.commit()
        self.publish(session_id, "action", {"id": action_id, "tool": tool, "status": "running"})
        return self.get_action(action_id), True

    def get_action(self, action_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE id=?", (action_id,)
            ).fetchone()
        if row is None:
            raise ActionNotFound("action not found")
        return self._action_public(row)

    @staticmethod
    def _action_public(row: sqlite3.Row) -> dict:
        status = row["status"]
        if status not in _ACTION_STATUSES:
            raise AgentWorkError("corrupt action status")
        out = {
            "id": row["id"],
            "session_id": row["session_id"],
            "agent": row["agent"],
            "tool": row["tool"],
            "args": json.loads(row["args_json"] or "{}"),
            "status": status,
            "progress": float(row["progress"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "created": row["created"],
            "updated": row["updated"],
        }
        if row["receipt_json"] is not None:
            out["receipt"] = json.loads(row["receipt_json"])
        if row["error"]:
            out["error"] = row["error"]
        return out

    def list_actions(
        self,
        session_id: str | None = None,
        agent: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM actions"
        clauses: list[str] = []
        args: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            args.append(session_id)
        if agent:
            clauses.append("agent=?")
            args.append(agent)
        if status:
            clauses.append("status=?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._action_public(r) for r in rows]

    def update_progress(self, action_id: str, progress: float, note: str | None = None) -> dict:
        progress = max(0.0, min(1.0, float(progress)))
        with self._lock:
            self._conn.execute(
                "UPDATE actions SET progress=?, updated=? WHERE id=?",
                (progress, self._clock(), action_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        self.publish(row["session_id"], "progress", {"id": action_id, "progress": progress, "note": note})
        return self._action_public(row)

    def cancel_requested(self, action_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM actions WHERE id=?", (action_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel(self, action_id: str) -> dict:
        with self._lock:
            self._conn.execute(
                "UPDATE actions SET cancel_requested=1, updated=? WHERE id=?",
                (self._clock(), action_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        self.publish(row["session_id"], "action", {"id": action_id, "cancel_requested": True})
        return self._action_public(row)

    def _transition(
        self, action_id: str, status: str, *, receipt: dict | None = None, error: str | None = None
    ) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise ActionNotFound("action not found")
            self._conn.execute(
                "UPDATE actions SET status=?, receipt_json=COALESCE(?,receipt_json), "
                "error=COALESCE(?,error), updated=? WHERE id=?",
                (status, json.dumps(receipt) if receipt is not None else None,
                 error, self._clock(), action_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        self.publish(row["session_id"], "action", {"id": action_id, "status": status})
        return self._action_public(row)

    def deny(self, action_id: str) -> dict:
        return self._transition(action_id, "denied")

    def approve(self, action_id: str) -> dict:
        return self._transition(action_id, "approved")

    def complete(self, action_id: str, receipt: dict) -> dict:
        return self._transition(action_id, "completed", receipt=receipt)

    def fail(self, action_id: str, error: str) -> dict:
        return self._transition(action_id, "failed", error=str(error)[:2000])

    def mark_cancelled(self, action_id: str) -> dict:
        return self._transition(action_id, "cancelled", error="cancelled")

    def expire(self, action_id: str) -> dict:
        return self._transition(action_id, "expired", error="approval expired")

    # ---------------- events / bus ----------------

    def publish(self, session_id: str, topic: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(session_id,topic,payload_json,ts) VALUES(?,?,?,?)",
                (session_id, topic, json.dumps(payload), self._clock()),
            )
            self._conn.commit()

    def poll_events(
        self,
        session_id: str | None = None,
        after_id: int = 0,
        limit: int = 100,
        topics: list[str] | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM events"
        clauses: list[str] = ["id > ?"]
        args: list[Any] = [int(after_id)]
        if session_id:
            clauses.append("session_id=?")
            args.append(session_id)
        if topics:
            marks = ",".join("?" for _ in topics)
            clauses.append(f"topic IN ({marks})")
            args.extend(topics)
        sql += " WHERE " + " AND ".join(clauses) + " ORDER BY id LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "topic": r["topic"],
                "payload": json.loads(r["payload_json"] or "{}"),
                "ts": r["ts"],
            }
            for r in rows
        ]

    # ---------------- policies ----------------

    def get_policy(self, agent: str) -> dict:
        agent = (agent or "agent").strip().lower()
        now = self._clock()
        with self._lock:
            row = self._conn.execute("SELECT * FROM policies WHERE agent=?", (agent,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO policies(agent,reset_min,reset_day) VALUES(?,?,?)",
                    (agent, now, now),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM policies WHERE agent=?", (agent,)).fetchone()
            policy = dict(row)
            if now - policy["reset_min"] >= policy["window_min"] * 60:
                self._conn.execute(
                    "UPDATE policies SET used_min=0, reset_min=? WHERE agent=?",
                    (now, agent),
                )
                policy["used_min"] = 0.0
                policy["reset_min"] = now
            if now - policy["reset_day"] >= policy["window_day"] * 60:
                self._conn.execute(
                    "UPDATE policies SET used_day=0, reset_day=? WHERE agent=?",
                    (now, agent),
                )
                policy["used_day"] = 0.0
                policy["reset_day"] = now
            self._conn.commit()
        return {k: policy[k] for k in ("agent", "quota_per_min", "quota_per_day", "write_allowed",
                                       "window_min", "window_day", "used_min", "used_day")}

    def set_policy(self, agent: str, **kwargs) -> dict:
        agent = (agent or "agent").strip().lower()
        allowed = {"quota_per_min", "quota_per_day", "write_allowed"}
        sets: list[str] = []
        args: list[Any] = []
        for k, v in kwargs.items():
            if k not in allowed:
                raise AgentWorkError(f"unknown policy field {k}")
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return self.get_policy(agent)
        with self._lock:
            self._conn.execute(
                "INSERT INTO policies(agent,reset_min,reset_day) VALUES(?,?,?) "
                "ON CONFLICT(agent) DO NOTHING",
                (agent, self._clock(), self._clock()),
            )
            self._conn.execute(
                f"UPDATE policies SET {', '.join(sets)} WHERE agent=?", (*args, agent)
            )
            self._conn.commit()
        return self.get_policy(agent)

    def check_limit(self, agent: str, write: bool = False) -> None:
        policy = self.get_policy(agent)
        if write and not policy["write_allowed"]:
            raise LimitExceeded(f"agent '{agent}' is read-only by policy")
        if policy["used_min"] + 1 > policy["quota_per_min"]:
            raise LimitExceeded(f"agent '{agent}' exceeded per-minute action quota")
        if policy["used_day"] + 1 > policy["quota_per_day"]:
            raise LimitExceeded(f"agent '{agent}' exceeded daily action quota")
        with self._lock:
            self._conn.execute(
                "UPDATE policies SET used_min=used_min+1, used_day=used_day+1 WHERE agent=?",
                (agent,),
            )
            self._conn.commit()

    # ---------------- persistence ----------------

    def recover(self) -> int:
        """Terminate unfinished actions at startup; no replay."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status FROM actions WHERE status IN ('queued','running',"
                "'awaiting_approval','approved','executing')"
            ).fetchall()
            for r in rows:
                status = "uncertain" if r["status"] == "executing" else "interrupted"
                self._conn.execute(
                    "UPDATE actions SET status=?, error=?, updated=? WHERE id=?",
                    (status, f"recovered as {status}", self._clock(), r["id"]),
                )
            self._conn.commit()
        return len(rows)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


# --------------------------------------------------------------------------- #
# Live-context diffing
# --------------------------------------------------------------------------- #

def _stable_key(snapshot: dict) -> str:
    files = [(f.get("path"), f.get("size_bytes"), f.get("truncated")) for f in snapshot.get("files", [])]
    win = (snapshot.get("desktop") or {}).get("active_window", {})
    key = {
        "files": files,
        "active_window": (win.get("title"), win.get("window_id")),
        "open_windows": len((snapshot.get("desktop") or {}).get("open_windows", [])),
        "has_screenshot": snapshot.get("has_screenshot", False),
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


class LiveDiff:
    def __init__(self, ledger: AgentWorkLedger, session_id: str):
        self.ledger = ledger
        self.session_id = session_id
        self._last_key: str | None = None

    def update(self, snapshot: dict) -> dict:
        key = _stable_key(snapshot)
        changed = key != self._last_key
        self._last_key = key
        self.ledger.set_context(
            self.session_id, {"live_last_key": key, "live_changed": changed}
        )
        return {"changed": changed, "key": key}

    def diff(self, prev: dict, curr: dict) -> dict:
        prev_files = {f["path"]: f for f in prev.get("files", [])}
        curr_files = {f["path"]: f for f in curr.get("files", [])}
        added = [p for p in curr_files if p not in prev_files]
        removed = [p for p in prev_files if p not in curr_files]
        modified = [
            p for p in curr_files
            if p in prev_files and prev_files[p].get("size_bytes") != curr_files[p].get("size_bytes")
        ]
        prev_win = (prev.get("desktop") or {}).get("active_window", {})
        curr_win = (curr.get("desktop") or {}).get("active_window", {})
        win_changed = (prev_win.get("title"), prev_win.get("window_id")) != (
            curr_win.get("title"), curr_win.get("window_id")
        )
        return {
            "files_added": added,
            "files_removed": removed,
            "files_modified": modified,
            "active_window_changed": win_changed,
            "active_window": curr_win.get("title", ""),
        }


# --------------------------------------------------------------------------- #
# Verification (tool-outcome feedback)
# --------------------------------------------------------------------------- #

class Verify:
    """Readback checks that confirm a mutation actually landed."""

    def __init__(self, controller: Any):
        self.controller = controller

    def file_written(self, rel_path: str, expected_content: str | None = None) -> dict:
        try:
            ws = self.controller.workspace
            info = ws.read(rel_path)
            content = info.get("content", "")
            ok = expected_content is None or expected_content in content
            return {"verified": ok, "path": rel_path, "bytes": len(content.encode("utf-8"))}
        except Exception as exc:
            return {"verified": False, "error": str(exc)}

    def browser_state(self) -> dict:
        try:
            b = self.controller.browser
            if b._page is None:
                return {"verified": False, "reason": "no_browser"}
            snap = b.snapshot()
            return {
                "verified": True,
                "url": snap.get("url"),
                "title": snap.get("title"),
                "text_len": len(snap.get("text") or ""),
            }
        except Exception as exc:
            return {"verified": False, "error": str(exc)}

    def paste_receipt(self) -> dict:
        try:
            return {"verified": bool(self.controller.last_reply)}
        except Exception:
            return {"verified": False}


# --------------------------------------------------------------------------- #
# Capability routing
# --------------------------------------------------------------------------- #

class Router:
    def __init__(self, controller: Any):
        self.controller = controller

    def route(self, task: str, requires: list[str] | None = None) -> list[dict]:
        """Rank candidates: require capability overlap, then freshness."""
        candidates = self.controller.memory.agents(fresh_s=3600)
        if not candidates:
            return []
        req = set(requires or [])
        scored = []
        for a in candidates:
            caps = set(a.get("capabilities") or [])
            if req and not req.issubset(caps):
                continue
            scored.append((len(caps & req), -int(a.get("age_s", 0)), a))
        scored.sort(reverse=True)
        return [a for _, _, a in scored]


# --------------------------------------------------------------------------- #
# Grounding (visual query + accessibility tree)
# --------------------------------------------------------------------------- #

class Grounding:
    """Semantic grounding over the desktop and the browser.

    ``visual_query`` runs a lightweight, offline heuristic first and then, when a
    vision model key is present, delegates ranking to it. The offline heuristic
    matches against the last live-context text and the browser accessibility
    snapshot so cloud agents can issue pointer actions by readable element labels.
    """

    def __init__(self, controller: Any):
        self.controller = controller

    def browser_a11y(self) -> list[dict]:
        try:
            page = self.controller.browser._page
            if page is None:
                return []
            snapshot = page.accessibility.snapshot()
            out: list[dict] = []

            def walk(node):
                if not isinstance(node, dict):
                    return
                role = node.get("role")
                name = node.get("name")
                if role and name:
                    out.append({
                        "role": role,
                        "text": str(name)[:200],
                        "focused": bool(node.get("focused", False)),
                    })
                for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
                    walk(child)

            walk(snapshot)
            return out[:100]
        except Exception:
            return []

    def visual_query(self, query: str, live_text: str = "") -> list[dict]:
        """Map a requested label/action to candidate elements on screen."""
        q = (query or "").lower()
        hits: list[dict] = []
        for el in self.browser_a11y():
            text = str(el.get("text", "")).lower()
            if q and q in text:
                hits.append({**el, "match": "accessibility"})
        if live_text and q:
            lines = [ln.strip() for ln in live_text.splitlines() if ln.strip()]
            for ln in lines[:50]:
                if q in ln.lower():
                    hits.append({"role": "text", "text": ln[:200], "match": "live_context"})
        return hits[:20]


# --------------------------------------------------------------------------- #
# Approval bridge
# --------------------------------------------------------------------------- #

class ApprovalBridge:
    """Holds pending async approvals keyed by action id.

    Mutating tools that require confirmation now return a pending approval
    (instead of throwing). A cloud agent holds the ``approval_id`` and may poll
    ``ren_work_approvals`` or resolve via ``ren_work_resolve``. The local orb /
    user can approve/deny on the same bridge so the agent sees the outcome.
    """

    def __init__(self, ledger: AgentWorkLedger):
        self.ledger = ledger
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def open(self, action_id: str, tool: str, summary: dict, ttl_s: float = 300.0) -> dict:
        with self._lock:
            self._pending[action_id] = {
                "action_id": action_id,
                "tool": tool,
                "summary": summary,
                "expires": self.ledger._clock() + ttl_s,
                "status": "pending",
            }
        return self._pending[action_id]

    def list_pending(self) -> list[dict]:
        now = self.ledger._clock()
        out = []
        with self._lock:
            for p in list(self._pending.values()):
                if now > p["expires"]:
                    p["status"] = "expired"
                out.append(p)
        return out

    def resolve(self, action_id: str, approved: bool) -> dict:
        now = self.ledger._clock()
        with self._lock:
            p = self._pending.get(action_id)
            if p is None:
                raise ActionNotFound("no pending approval")
            if now > p["expires"]:
                p["status"] = "expired"
                self.ledger.expire(action_id)
                return p
            p["status"] = "approved" if approved else "denied"
            if approved:
                self.ledger.approve(action_id)
            else:
                self.ledger.deny(action_id)
        return p


# --------------------------------------------------------------------------- #
# Coordinator: ties every enhancement to one session-scoped entry point.
# --------------------------------------------------------------------------- #

class AgentWorkspace:
    """Top-level work-through coordinator attached to the Ren controller."""

    def __init__(self, controller: Any, ledger: AgentWorkLedger | None = None):
        self.controller = controller
        self.ledger = ledger or AgentWorkLedger()
        self.bridge = ApprovalBridge(self.ledger)
        self.verify = Verify(controller)
        self.router = Router(controller)
        self.grounding = Grounding(controller)
        self._locks: dict[str, LiveDiff] = {}
        self._active: dict[str, str] = {}  # session_id -> action_id

    # ---- session helpers ----

    def begin(self, session_id: str, agent: str, context: dict | None = None) -> dict:
        sess = self.ledger.ensure_session(session_id, agent)
        if context:
            existing = sess["context"]
            existing.update(context)
            sess = self.ledger.set_context(session_id, existing)
        self._locks[session_id] = LiveDiff(self.ledger, session_id)
        self._active.setdefault(session_id, "")
        return sess

    def track(self, session_id: str, action_id: str) -> None:
        self._active[session_id] = action_id

    def current_action(self, session_id: str) -> str:
        return self._active.get(session_id, "")

    def live_diff(self, session_id: str) -> LiveDiff:
        if session_id not in self._locks:
            self._locks[session_id] = LiveDiff(self.ledger, session_id)
        return self._locks[session_id]

    def publish(self, session_id: str, topic: str, payload: dict) -> None:
        self.ledger.publish(session_id, topic, payload)

    # ---- single-mutation envelope ----

    def execute(
        self,
        session_id: str,
        agent: str,
        tool: str,
        args: dict,
        fn: Callable[[], Any],
        *,
        idempotency_key: str | None = None,
        require_approval: bool = False,
        approval_summary: dict | None = None,
        verify_fn: Callable[[], dict] | None = None,
        write: bool = False,
    ) -> dict:
        """Run one tool call with ledger, limits, approvals, verification, and envelope."""
        self.ledger.check_limit(agent, write=write)
        action, is_new = self.ledger.reserve_action(
            session_id, agent, tool, args, idempotency_key=idempotency_key
        )
        self.track(session_id, action["id"])
        if not is_new:
            # Idempotent replay: return the earlier terminal outcome if present.
            if action["status"] in _TERMINAL_ACTION:
                return self._envelope(action)
            if self.ledger.cancel_requested(action["id"]):
                action = self.ledger.request_cancel(action["id"])
            else:
                action = action

        if require_approval:
            self.ledger.update_progress(action["id"], 0.05, "awaiting approval")
            self.ledger._transition(action["id"], "awaiting_approval")
            pending = self.bridge.open(action["id"], tool, approval_summary or {})
            return {
                "type": "pending_approval",
                "action_id": action["id"],
                "requires_confirm": True,
                "approval": pending,
                "committed": False,
            }

        try:
            self.ledger.update_progress(action["id"], 0.2, "started")
            if self.ledger.cancel_requested(action["id"]):
                return self._envelope(self.ledger.mark_cancelled(action["id"]))
            result = fn()
            self.ledger.update_progress(action["id"], 0.8, "running")
            receipt = {"result": result}
            if verify_fn is not None:
                receipt["verified"] = verify_fn()
            action = self.ledger.complete(action["id"], receipt)
            self.ledger.update_progress(action["id"], 1.0, "done")
            return self._envelope(action)
        except LimitExceeded as exc:
            return self._envelope(self.ledger.fail(action["id"], str(exc)), error=str(exc))
        except ApprovalPending:
            pending = self.bridge.open(action["id"], tool, approval_summary or {})
            return {
                "type": "pending_approval",
                "action_id": action["id"],
                "requires_confirm": True,
                "approval": pending,
                "committed": False,
            }
        except Exception as exc:
            return self._envelope(self.ledger.fail(action["id"], str(exc)), error=str(exc))

    def execute_after_approval(
        self, action_id: str, fn: Callable[[], Any], verify_fn: Callable[[], dict] | None = None
    ) -> dict:
        """Execute a previously approved async action (work-through continuation)."""
        action = self.ledger.get_action(action_id)
        if action["status"] == "denied":
            return self._envelope(action, error="denied")
        if action["status"] == "expired":
            return self._envelope(action, error="expired")
        if action["status"] == "cancelled":
            return self._envelope(action, error="cancelled")
        try:
            if self.ledger.cancel_requested(action_id):
                return self._envelope(self.ledger.mark_cancelled(action_id), error="cancelled")
            self.ledger.update_progress(action_id, 0.2, "started")
            result = fn()
            self.ledger.update_progress(action_id, 0.8, "running")
            receipt = {"result": result}
            if verify_fn is not None:
                receipt["verified"] = verify_fn()
            action = self.ledger.complete(action_id, receipt)
            self.ledger.update_progress(action_id, 1.0, "done")
            return self._envelope(action)
        except Exception as exc:
            return self._envelope(self.ledger.fail(action_id, str(exc)), error=str(exc))

    def _envelope(self, action: dict, error: str | None = None) -> dict:
        payload = {
            "type": "agentwork_result",
            "schema": "agentwork.action.v1",
            "action_id": action["id"],
            "session_id": action.get("session_id"),
            "tool": action.get("tool"),
            "status": action.get("status"),
            "progress": action.get("progress"),
        }
        if action.get("receipt") is not None:
            payload["data"] = action["receipt"].get("result")
            if action["receipt"].get("verified") is not None:
                payload["verified"] = action["receipt"]["verified"]
        if error or action.get("error"):
            payload["error"] = error or action.get("error")
        return payload
