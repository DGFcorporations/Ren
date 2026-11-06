"""Durable, tenant-scoped SQLite state for the synthetic G1 offline pilot.

No provider, network, or browser access occurs here.  The controller is responsible
for authorizing fact mutations.  Call ``recover()`` once at application startup,
*before* accepting work; opening another connection is deliberately not a claim
that an existing worker has crashed.  Recovery must not race a live executor.

Each write is a BEGIN IMMEDIATE transaction, also protected by a connection-local
RLock.  Approval rejection that changes durable state is raised only AFTER its
transaction commits.  Decisions and execution claims are never replayable.

``decide``, ``begin``, and ``complete`` return action dictionaries; ``cancel`` and
``fail`` return run dictionaries.  Receipts live on the run.  ``search_backend``
explicitly reports ``fts5`` or the same-owner ``like`` fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any


class LedgerError(ValueError):
    """Invalid input, corrupt storage, or an unavailable ledger."""


class Conflict(LedgerError):
    """The requested mutation conflicts with durable state."""


class StaleApproval(Conflict):
    """An approval is expired, changed, consumed, or otherwise unusable."""


class NotFound(LedgerError):
    """The record does not exist in the supplied owner's scope."""


_APPLICATION_ID = 0x47314C47  # G1LG; do not adopt unrelated SQLite databases.
_SCHEMA_VERSION = 1
_MAX_JSON_BYTES = 32 * 1024
_MAX_SEARCH_LIMIT = 100
_SOURCE = "synthetic_fixture"
_ACTIVE = frozenset({"preparing", "pending", "approved", "executing"})
_TERMINAL = frozenset(
    {
        "completed",
        "denied",
        "cancelled",
        "stale",
        "expired",
        "failed",
        "uncertain",
        "interrupted",
    }
)
_RUN_STATUSES = _ACTIVE | _TERMINAL
_ACTION_STATUSES = _RUN_STATUSES - {"preparing"}
_REQUIRED_TABLES = frozenset(
    {
        "g1_facts",
        "g1_fact_revisions",
        "g1_runs",
        "g1_actions",
        "g1_events",
    }
)
_LOG = logging.getLogger(__name__)


def _text(value: Any, name: str, *, maximum: int | None = None, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LedgerError(f"{name} must be a string.")
    if not empty and not value.strip():
        raise LedgerError(f"{name} must not be empty.")
    if maximum is not None and len(value) > maximum:
        raise LedgerError(f"{name} exceeds {maximum} characters.")
    if "\x00" in value:
        raise LedgerError(f"{name} must not contain NUL characters.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise LedgerError(f"{name} must be valid Unicode.") from exc
    return value


def _json_object(value: Any, name: str) -> str:
    """Canonical UTF-8 JSON, without coercions, NaN, cycles, or unbounded depth."""
    if not isinstance(value, dict):
        raise LedgerError(f"{name} must be a JSON object.")
    ancestors: set[int] = set()
    visited = 0

    def check(item: Any, depth: int) -> None:
        nonlocal visited
        visited += 1
        if visited > 10000 or depth > 64:
            raise LedgerError(f"{name} is too complex.")
        if item is None or type(item) in (bool, int):
            return
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise LedgerError(f"{name} contains invalid Unicode.") from exc
            if len(item) > _MAX_JSON_BYTES:
                raise LedgerError(f"{name} exceeds 32 KiB.")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise LedgerError(f"{name} contains a non-finite number.")
            return
        if not isinstance(item, (dict, list)):
            raise LedgerError(f"{name} contains a non-JSON value.")
        identity = id(item)
        if identity in ancestors:
            raise LedgerError(f"{name} contains a cycle.")
        ancestors.add(identity)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise LedgerError(f"{name} keys must be strings.")
                    check(key, depth + 1)
                    check(child, depth + 1)
            else:
                for child in item:
                    check(child, depth + 1)
        finally:
            ancestors.remove(identity)

    try:
        check(value, 0)
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError, RuntimeError) as exc:
        if isinstance(exc, LedgerError):
            raise
        raise LedgerError(f"{name} is not valid JSON.") from exc
    if size > _MAX_JSON_BYTES:
        raise LedgerError(f"{name} exceeds 32 KiB.")
    return encoded


def _digest(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _digest_matches(canonical: str, stored: Any) -> bool:
    # compare_digest(str, str) itself raises on non-ASCII corruption; validate
    # before comparing so malformed storage always fails as a public LedgerError.
    return (
        isinstance(stored, str)
        and re.fullmatch(r"[0-9a-f]{64}", stored) is not None
        and hmac.compare_digest(_digest(canonical), stored)
    )


def _stored_object(raw: Any, name: str) -> dict:
    try:
        if not isinstance(raw, str):
            raise ValueError("not text")
        # Avoid parsing arbitrarily large or maliciously deep stored records.
        if len(raw.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError("too large")
        parsed = json.loads(raw)
        if _json_object(parsed, name) != raw:
            raise ValueError("not canonical")
        return parsed
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise LedgerError(f"Corrupt {name} record.") from exc


def _finite_number(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise LedgerError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise LedgerError(f"{name} must be a finite number.") from exc
    if not math.isfinite(result):
        raise LedgerError(f"{name} must be a finite number.")
    return result


def _expected(value: Any) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise LedgerError("expected_revision must be a nonnegative integer or None.")
    return value


class Ledger:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        if not isinstance(path, Path):
            raise LedgerError("path must be a pathlib.Path for a dedicated G1 database.")
        if not callable(clock):
            raise LedgerError("clock must be callable.")
        self._clock = clock
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        self.search_backend = "like"
        self._now()  # Reject an invalid clock before creating any database.
        try:
            self._conn = sqlite3.connect(
                str(path),
                timeout=10.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._initialize()
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.close()
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("Could not open a valid dedicated G1 ledger.") from exc

    def _now(self) -> float:
        try:
            now = self._clock()
        except Exception as exc:
            raise LedgerError("The ledger clock could not be read.") from exc
        return _finite_number(now, "clock result")

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._conn
            if conn is None:
                raise LedgerError("Ledger is closed.")
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield conn
                conn.commit()
            except BaseException as exc:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                except sqlite3.Error:
                    # Do not reuse a connection whose transaction state is unknown.
                    self._conn = None
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                if isinstance(exc, sqlite3.Error):
                    raise LedgerError("SQLite ledger transaction failed.") from exc
                raise

    @staticmethod
    def _schema_state(conn: sqlite3.Connection) -> bool:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        }
        if app_id == 0 and version == 0 and not names:
            return False
        if app_id != _APPLICATION_ID:
            raise LedgerError("Refusing to reuse a database that is not a G1 ledger.")
        if version != _SCHEMA_VERSION or not _REQUIRED_TABLES.issubset(names):
            raise LedgerError("Unsupported or incomplete G1 ledger schema.")
        return True

    def _initialize(self) -> None:
        conn = self._conn
        assert conn is not None
        # Inspect identity BEFORE any persistent PRAGMA or schema mutation.
        self._schema_state(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise LedgerError("WAL mode is required for the G1 ledger.")
        conn.execute("PRAGMA synchronous=FULL")
        with self._transaction() as conn:
            exists = self._schema_state(conn)
            if not exists:
                run_statuses = ",".join(repr(s) for s in sorted(_RUN_STATUSES))
                action_statuses = ",".join(repr(s) for s in sorted(_ACTION_STATUSES))
                statements = [
                    """CREATE TABLE g1_facts (
                        owner TEXT NOT NULL, key TEXT NOT NULL,
                        value TEXT, revision INTEGER NOT NULL CHECK(revision >= 1),
                        deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
                        source TEXT NOT NULL CHECK(source = 'synthetic_fixture'),
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(owner,key),
                        CHECK(length(owner) > 0 AND length(key) BETWEEN 1 AND 120),
                        CHECK((deleted=1 AND value IS NULL) OR
                              (deleted=0 AND value IS NOT NULL AND length(value)<=4000))
                    )""",
                    """CREATE TABLE g1_fact_revisions (
                        owner TEXT NOT NULL, key TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision >= 1), value TEXT,
                        deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
                        source TEXT NOT NULL CHECK(source = 'synthetic_fixture'),
                        timestamp REAL NOT NULL,
                        PRIMARY KEY(owner,key,revision),
                        FOREIGN KEY(owner,key) REFERENCES g1_facts(owner,key),
                        CHECK((deleted=1 AND value IS NULL) OR
                              (deleted=0 AND value IS NOT NULL AND length(value)<=4000))
                    )""",
                    f"""CREATE TABLE g1_runs (
                        id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ({run_statuses})),
                        action_id TEXT UNIQUE REFERENCES g1_actions(id)
                            DEFERRABLE INITIALLY DEFERRED,
                        receipt_json TEXT, reason TEXT,
                        cancel_requested INTEGER NOT NULL DEFAULT 0
                            CHECK(cancel_requested IN (0,1)),
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        UNIQUE(owner,request_id), UNIQUE(id,owner),
                        CHECK(length(owner)>0 AND length(request_id) BETWEEN 1 AND 100)
                    )""",
                    f"""CREATE TABLE g1_actions (
                        id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                        owner TEXT NOT NULL, proposal_json TEXT NOT NULL,
                        digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ({action_statuses})),
                        expires_at REAL NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        FOREIGN KEY(run_id,owner) REFERENCES g1_runs(id,owner),
                        CHECK(expires_at > created_at)
                    )""",
                    """CREATE TABLE g1_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner TEXT NOT NULL, run_id TEXT NOT NULL,
                        action_id TEXT REFERENCES g1_actions(id),
                        event TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        FOREIGN KEY(run_id,owner) REFERENCES g1_runs(id,owner)
                    )""",
                    "CREATE INDEX g1_events_run ON g1_events(owner,run_id,id)",
                    "CREATE INDEX g1_runs_status ON g1_runs(status)",
                ]
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LedgerError("G1 ledger failed its SQLite integrity check.")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LedgerError("G1 ledger contains invalid record associations.")
            self._configure_search(conn)

    def _configure_search(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS g1_facts_fts
                         USING fts5(owner UNINDEXED, key, value, tokenize='unicode61')""")
            # Derived index; rebuilding also makes switching from LIKE safe.
            conn.execute("DELETE FROM g1_facts_fts")
            conn.execute("""INSERT INTO g1_facts_fts(owner,key,value)
                         SELECT owner,key,value FROM g1_facts WHERE deleted=0""")
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc).lower():
                raise
            self.search_backend = "like"
            _LOG.warning("FTS5 unavailable; using bounded, same-owner LIKE fact search.")
        else:
            self.search_backend = "fts5"

    @staticmethod
    def _fact_public(row: sqlite3.Row) -> dict:
        _text(row["owner"], "stored owner")
        _text(row["key"], "stored key", maximum=120)
        if type(row["revision"]) is not int or row["revision"] < 1:
            raise LedgerError("Corrupt fact revision.")
        if type(row["deleted"]) is not int or row["deleted"] not in (0, 1):
            raise LedgerError("Corrupt fact tombstone.")
        if row["source"] != _SOURCE:
            raise LedgerError("Corrupt fact provenance.")
        if row["deleted"]:
            if row["value"] is not None:
                raise LedgerError("Corrupt tombstone value.")
        else:
            _text(row["value"], "stored value", maximum=4000, empty=True)
        return {
            "key": row["key"],
            "value": row["value"],
            "revision": row["revision"],
            "deleted": bool(row["deleted"]),
            "source": row["source"],
        }

    @staticmethod
    def _check_revision(row: sqlite3.Row | None, expected_revision: int | None) -> None:
        actual = 0 if row is None else row["revision"]
        if expected_revision is not None and expected_revision != actual:
            raise Conflict("Fact revision changed.")

    def _write_fact(
        self,
        conn: sqlite3.Connection,
        owner: str,
        key: str,
        value: str | None,
        revision: int,
        deleted: bool,
        now: float,
    ) -> dict:
        conn.execute(
            """INSERT INTO g1_facts
                     (owner,key,value,revision,deleted,source,updated_at)
                     VALUES (?,?,?,?,?,?,?)
                     ON CONFLICT(owner,key) DO UPDATE SET
                     value=excluded.value, revision=excluded.revision,
                     deleted=excluded.deleted, source=excluded.source,
                     updated_at=excluded.updated_at""",
            (owner, key, value, revision, int(deleted), _SOURCE, now),
        )
        conn.execute(
            """INSERT INTO g1_fact_revisions
                     (owner,key,revision,value,deleted,source,timestamp)
                     VALUES (?,?,?,?,?,?,?)""",
            (owner, key, revision, value, int(deleted), _SOURCE, now),
        )
        if self.search_backend == "fts5":
            conn.execute("DELETE FROM g1_facts_fts WHERE owner=? AND key=?", (owner, key))
            if not deleted:
                conn.execute("INSERT INTO g1_facts_fts(owner,key,value) VALUES (?,?,?)", (owner, key, value))
        row = conn.execute("SELECT * FROM g1_facts WHERE owner=? AND key=?", (owner, key)).fetchone()
        return self._fact_public(row)

    def seed_facts(self, owner: str, items: dict[str, str]) -> None:
        _text(owner, "owner")
        if not isinstance(items, dict):
            raise LedgerError("items must be a dictionary of synthetic facts.")
        # Validate every item before inserting any of them.
        checked = [
            (_text(k, "key", maximum=120), _text(v, "value", maximum=4000, empty=True))
            for k, v in items.items()
        ]
        with self._transaction() as conn:
            now = self._now()
            for key, value in checked:
                row = conn.execute("SELECT * FROM g1_facts WHERE owner=? AND key=?", (owner, key)).fetchone()
                if row is None:
                    self._write_fact(conn, owner, key, value, 1, False, now)
                else:
                    self._fact_public(row)

    def get_fact(self, owner: str, key: str) -> dict | None:
        _text(owner, "owner")
        _text(key, "key", maximum=120)
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM g1_facts WHERE owner=? AND key=?", (owner, key)).fetchone()
            if row is None:
                return None
            result = self._fact_public(row)
            return None if result["deleted"] else result

    def get_fact_state(self, owner: str, key: str) -> dict | None:
        """Owner-scoped revision metadata, including tombstones; deleted values stay purged."""
        _text(owner, "owner")
        _text(key, "key", maximum=120)
        with self._transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM g1_facts WHERE owner=? AND key=?", (owner, key)).fetchone()
            return None if row is None else self._fact_public(row)

    def list_facts(self, owner: str) -> list[dict]:
        _text(owner, "owner")
        with self._transaction(write=False) as conn:
            return [
                self._fact_public(row)
                for row in conn.execute(
                    "SELECT * FROM g1_facts WHERE owner=? AND deleted=0 ORDER BY key", (owner,)
                )
            ]

    def search_facts(self, owner: str, query: str, limit: int = 10) -> list[dict]:
        _text(owner, "owner")
        _text(query, "query", maximum=1000, empty=True)
        if type(limit) is not int or not 1 <= limit <= _MAX_SEARCH_LIMIT:
            raise LedgerError("limit must be an integer between 1 and 100.")
        terms = list(dict.fromkeys(re.findall(r"\w+", query, flags=re.UNICODE)))
        if len(terms) > 32:
            raise LedgerError("query must contain at most 32 distinct keywords.")
        with self._transaction(write=False) as conn:
            if not terms:
                return []
            if self.search_backend == "fts5":
                # Caller text is never interpreted as FTS operators or column selectors.
                match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = conn.execute(
                    """SELECT f.* FROM g1_facts_fts
                    JOIN g1_facts f ON f.owner=g1_facts_fts.owner AND f.key=g1_facts_fts.key
                    WHERE g1_facts_fts MATCH ? AND f.owner=? AND f.deleted=0
                    ORDER BY bm25(g1_facts_fts), f.key LIMIT ?""",
                    (match, owner, limit),
                )
            else:
                clauses = []
                params: list[Any] = [owner]
                for term in terms:
                    literal = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    clauses.append("(key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')")
                    params.extend((f"%{literal}%", f"%{literal}%"))
                params.append(limit)
                sql = (
                    "SELECT * FROM g1_facts WHERE owner=? AND deleted=0 AND "
                    + " AND ".join(clauses)
                    + " ORDER BY key LIMIT ?"
                )
                rows = conn.execute(sql, params)
            return [self._fact_public(row) for row in rows]

    def set_fact(self, owner: str, key: str, value: str, *, expected_revision: int | None = None) -> dict:
        _text(owner, "owner")
        _text(key, "key", maximum=120)
        _text(value, "value", maximum=4000, empty=True)
        _expected(expected_revision)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM g1_facts WHERE owner=? AND key=?", (owner, key)).fetchone()
            if row is not None:
                self._fact_public(row)
            self._check_revision(row, expected_revision)
            revision = 1 if row is None else row["revision"] + 1
            return self._write_fact(conn, owner, key, value, revision, False, self._now())

    def delete_fact(self, owner: str, key: str, *, expected_revision: int | None = None) -> dict:
        _text(owner, "owner")
        _text(key, "key", maximum=120)
        _expected(expected_revision)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM g1_facts WHERE owner=? AND key=?", (owner, key)).fetchone()
            if row is not None:
                result = self._fact_public(row)
            self._check_revision(row, expected_revision)
            if row is None:
                raise NotFound("Fact not found.")
            if row["deleted"]:
                return result  # Idempotent deletion; it never restores an old value.
            return self._write_fact(conn, owner, key, None, row["revision"] + 1, True, self._now())

    @staticmethod
    def _action_public(row: sqlite3.Row) -> dict:
        proposal = _stored_object(row["proposal_json"], "proposal")
        if set(proposal) != {"kind", "args", "binding"}:
            raise LedgerError("Corrupt proposal fields.")
        _text(proposal["kind"], "stored kind", maximum=120)
        if not isinstance(proposal["args"], dict) or not isinstance(proposal["binding"], dict):
            raise LedgerError("Corrupt proposal arguments or binding.")
        if not _digest_matches(row["proposal_json"], row["digest"]):
            raise LedgerError("Corrupt proposal digest.")
        if row["status"] not in _ACTION_STATUSES:
            raise LedgerError("Corrupt action status.")
        expiry = _finite_number(row["expires_at"], "stored expiry")
        created = _finite_number(row["created_at"], "stored action creation time")
        if expiry <= created:
            raise LedgerError("Corrupt action expiry.")
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "owner": row["owner"],
            "kind": proposal["kind"],
            "args": proposal["args"],
            "binding": proposal["binding"],
            "digest": row["digest"],
            "status": row["status"],
            "expires_at": expiry,
        }

    def _run_state(
        self, conn: sqlite3.Connection, owner: str, run_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row | None, dict]:
        run = conn.execute("SELECT * FROM g1_runs WHERE owner=? AND id=?", (owner, run_id)).fetchone()
        if run is None:
            raise NotFound("Run not found.")
        _stored_object(run["payload_json"], "request payload")
        if not _digest_matches(run["payload_json"], run["payload_digest"]):
            raise LedgerError("Corrupt request payload digest.")
        if run["status"] not in _RUN_STATUSES:
            raise LedgerError("Corrupt run status.")
        if type(run["cancel_requested"]) is not int or run["cancel_requested"] not in (0, 1):
            raise LedgerError("Corrupt cancellation state.")
        _text(run["request_id"], "stored request_id", maximum=100)
        if run["reason"] is not None:
            _text(run["reason"], "stored reason", maximum=4000)
        action = None
        proposal = None
        if run["action_id"] is not None:
            action = conn.execute(
                "SELECT * FROM g1_actions WHERE id=? AND run_id=? AND owner=?",
                (run["action_id"], run_id, owner),
            ).fetchone()
            if action is None or action["status"] != run["status"]:
                raise LedgerError("Corrupt run/action association or status.")
            proposal = self._action_public(action)
        elif run["status"] in {"pending", "approved", "executing", "completed", "denied", "stale", "expired"}:
            raise LedgerError("Run is missing its required action.")
        receipt = None
        if run["receipt_json"] is not None:
            receipt = _stored_object(run["receipt_json"], "receipt")
        if (run["status"] == "completed") != (receipt is not None):
            raise LedgerError("Corrupt completed receipt state.")
        public = {
            "id": run["id"],
            "owner": run["owner"],
            "request_id": run["request_id"],
            "status": run["status"],
            "action_id": run["action_id"],
            "proposal": proposal,
            "receipt": receipt,
            "reason": run["reason"],
            "cancel_requested": bool(run["cancel_requested"]),
        }
        return run, action, public

    def _action_state(
        self, conn: sqlite3.Connection, owner: str, action_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, dict]:
        ref = conn.execute(
            "SELECT run_id FROM g1_actions WHERE owner=? AND id=?", (owner, action_id)
        ).fetchone()
        if ref is None:
            raise NotFound("Action not found.")
        run, action, public = self._run_state(conn, owner, ref["run_id"])
        if action is None or action["id"] != action_id:
            raise LedgerError("Corrupt action/run association.")
        return run, action, public

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        *,
        owner: str,
        run_id: str,
        action_id: str | None,
        event: str,
        before: str | None,
        after: str,
        now: float,
    ) -> None:
        # No payload, binding, receipt, free-form reason, or fact value in events.
        conn.execute(
            """INSERT INTO g1_events
                     (owner,run_id,action_id,event,from_status,to_status,timestamp)
                     VALUES (?,?,?,?,?,?,?)""",
            (owner, run_id, action_id, event, before, after, now),
        )

    def _transition(
        self,
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        action: sqlite3.Row | None,
        status: str,
        event: str,
        now: float,
        *,
        reason: str | None = None,
        receipt: str | None = None,
        cancel_requested: bool | None = None,
    ) -> None:
        flag = None if cancel_requested is None else int(cancel_requested)
        conn.execute(
            """UPDATE g1_runs SET status=?, reason=?, updated_at=?,
                     receipt_json=COALESCE(?,receipt_json),
                     cancel_requested=COALESCE(?,cancel_requested) WHERE id=?""",
            (status, reason, now, receipt, flag, run["id"]),
        )
        if action is not None:
            conn.execute(
                "UPDATE g1_actions SET status=?,updated_at=? WHERE id=?", (status, now, action["id"])
            )
        self._event(
            conn,
            owner=run["owner"],
            run_id=run["id"],
            action_id=None if action is None else action["id"],
            event=event,
            before=run["status"],
            after=status,
            now=now,
        )

    def create_run(self, owner: str, request_id: str, payload: dict) -> tuple[dict, bool]:
        _text(owner, "owner")
        _text(request_id, "request_id", maximum=100)
        canonical = _json_object(payload, "payload")
        payload_digest = _digest(canonical)
        with self._transaction() as conn:
            prior = conn.execute(
                "SELECT id FROM g1_runs WHERE owner=? AND request_id=?", (owner, request_id)
            ).fetchone()
            if prior is not None:
                run, _, public = self._run_state(conn, owner, prior["id"])
                if run["payload_digest"] != payload_digest or run["payload_json"] != canonical:
                    raise Conflict("request_id is already bound to a different payload.")
                return public, False
            now = self._now()
            run_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO g1_runs
                         (id,owner,request_id,payload_json,payload_digest,status,created_at,updated_at)
                         VALUES (?,?,?,?,?,'preparing',?,?)""",
                (run_id, owner, request_id, canonical, payload_digest, now, now),
            )
            self._event(
                conn,
                owner=owner,
                run_id=run_id,
                action_id=None,
                event="run_created",
                before=None,
                after="preparing",
                now=now,
            )
            return self._run_state(conn, owner, run_id)[2], True

    def propose(self, run_id: str, kind: str, args: dict, binding: dict, *, ttl_seconds: float = 300) -> dict:
        _text(run_id, "run_id")
        _text(kind, "kind", maximum=120)
        _json_object(args, "args")
        _json_object(binding, "binding")
        canonical = _json_object({"kind": kind, "args": args, "binding": binding}, "proposal")
        ttl = _finite_number(ttl_seconds, "ttl_seconds")
        if ttl <= 0:
            raise LedgerError("ttl_seconds must be positive.")
        with self._transaction() as conn:
            ref = conn.execute("SELECT owner FROM g1_runs WHERE id=?", (run_id,)).fetchone()
            if ref is None:
                raise NotFound("Run not found.")
            run, action, _ = self._run_state(conn, ref["owner"], run_id)
            if run["status"] != "preparing" or action is not None or run["cancel_requested"]:
                raise Conflict("A proposal requires an uncancelled preparing run.")
            now = self._now()
            expiry = now + ttl
            if not math.isfinite(expiry) or expiry <= now:
                raise LedgerError("ttl_seconds does not produce a valid future expiry.")
            action_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO g1_actions
                         (id,run_id,owner,proposal_json,digest,status,expires_at,created_at,updated_at)
                         VALUES (?,?,?,?,?,'pending',?,?,?)""",
                (action_id, run_id, run["owner"], canonical, _digest(canonical), expiry, now, now),
            )
            conn.execute(
                "UPDATE g1_runs SET action_id=?,status='pending',updated_at=? WHERE id=?",
                (action_id, now, run_id),
            )
            self._event(
                conn,
                owner=run["owner"],
                run_id=run_id,
                action_id=action_id,
                event="action_proposed",
                before="preparing",
                after="pending",
                now=now,
            )
            return self._run_state(conn, run["owner"], run_id)[2]["proposal"]

    def get_run(self, owner: str, run_id: str) -> dict:
        _text(owner, "owner")
        _text(run_id, "run_id")
        with self._transaction(write=False) as conn:
            return self._run_state(conn, owner, run_id)[2]

    def get_action(self, owner: str, action_id: str) -> dict:
        _text(owner, "owner")
        _text(action_id, "action_id")
        with self._transaction(write=False) as conn:
            return self._action_state(conn, owner, action_id)[2]["proposal"]

    def decide(self, owner: str, action_id: str, approved: bool, *, digest: str) -> dict:
        _text(owner, "owner")
        _text(action_id, "action_id")
        if type(approved) is not bool:
            raise LedgerError("approved must be a boolean.")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise LedgerError("digest must be a SHA256 hexadecimal string.")
        rejection = None
        result = None
        with self._transaction() as conn:
            run, action, _ = self._action_state(conn, owner, action_id)
            if action["status"] != "pending":
                raise StaleApproval("This action no longer accepts a decision.")
            now = self._now()
            if run["cancel_requested"]:
                state, event, message = "cancelled", "run_cancelled", "Cancellation invalidated approval."
            elif now >= action["expires_at"]:
                state, event, message = "expired", "approval_expired", "Approval expired."
            elif not hmac.compare_digest(action["digest"], digest):
                state, event, message = "stale", "approval_stale", "Proposal digest changed."
            else:
                state = "approved" if approved else "denied"
                event = "approval_granted" if approved else "approval_denied"
                message = None
            self._transition(conn, run, action, state, event, now, reason=message)
            if message is not None:
                rejection = StaleApproval(message)
            else:
                result = self._action_state(conn, owner, action_id)[2]["proposal"]
        # Raising inside the transaction would silently undo expiry/staleness.
        if rejection is not None:
            raise rejection
        return result

    def begin(self, owner: str, action_id: str, *, current_binding: dict) -> dict:
        _text(owner, "owner")
        _text(action_id, "action_id")
        current = _json_object(current_binding, "current_binding")
        rejection = None
        result = None
        with self._transaction() as conn:
            run, action, public = self._action_state(conn, owner, action_id)
            if action["status"] != "approved":
                raise StaleApproval("Execution requires an unused approved action.")
            now = self._now()
            if run["cancel_requested"]:
                state, event, message = "cancelled", "run_cancelled", "Cancellation invalidated approval."
            elif now >= action["expires_at"]:
                state, event, message = "expired", "approval_expired", "Approval expired."
            elif current != _json_object(public["proposal"]["binding"], "stored binding"):
                state, event, message = "stale", "approval_stale", "Execution binding changed."
            else:
                state, event, message = "executing", "action_started", None
            self._transition(conn, run, action, state, event, now, reason=message)
            if message is not None:
                rejection = StaleApproval(message)
            else:
                result = self._action_state(conn, owner, action_id)[2]["proposal"]
        if rejection is not None:
            raise rejection
        return result

    def complete(self, owner: str, action_id: str, receipt: dict) -> dict:
        _text(owner, "owner")
        _text(action_id, "action_id")
        canonical = _json_object(receipt, "receipt")
        with self._transaction() as conn:
            run, action, _ = self._action_state(conn, owner, action_id)
            if action["status"] != "executing":
                raise Conflict("Only an executing action can be completed once.")
            self._transition(
                conn, run, action, "completed", "action_completed", self._now(), receipt=canonical
            )
            return self._action_state(conn, owner, action_id)[2]["proposal"]

    def fail(self, owner: str, run_id: str, reason: str, *, uncertain: bool = False) -> dict:
        _text(owner, "owner")
        _text(run_id, "run_id")
        _text(reason, "reason", maximum=4000)
        if type(uncertain) is not bool:
            raise LedgerError("uncertain must be a boolean.")
        with self._transaction() as conn:
            run, action, public = self._run_state(conn, owner, run_id)
            if run["status"] in _TERMINAL:
                return public  # Never rewrite completed evidence or other terminal decisions.
            state = "uncertain" if uncertain else "failed"
            self._transition(conn, run, action, state, "run_" + state, self._now(), reason=reason)
            return self._run_state(conn, owner, run_id)[2]

    def cancel(self, owner: str, run_id: str) -> dict:
        _text(owner, "owner")
        _text(run_id, "run_id")
        with self._transaction() as conn:
            run, action, public = self._run_state(conn, owner, run_id)
            if run["status"] in _TERMINAL or run["cancel_requested"]:
                return public
            if run["status"] == "executing":
                self._transition(
                    conn,
                    run,
                    action,
                    "executing",
                    "cancellation_requested",
                    self._now(),
                    cancel_requested=True,
                    reason="Cancellation requested during execution; no undo is asserted.",
                )
            else:
                self._transition(
                    conn,
                    run,
                    action,
                    "cancelled",
                    "run_cancelled",
                    self._now(),
                    cancel_requested=True,
                    reason="Cancelled before execution.",
                )
            return self._run_state(conn, owner, run_id)[2]

    def expire_run(self, owner: str, run_id: str) -> dict:
        """Expire unused authorizations without executing, denying another owner, or undoing work."""
        _text(owner, "owner")
        _text(run_id, "run_id")
        with self._transaction() as conn:
            run, action, public = self._run_state(conn, owner, run_id)
            if action is not None and run["status"] in {"pending", "approved"}:
                now = self._now()
                if now >= action["expires_at"]:
                    self._transition(
                        conn,
                        run,
                        action,
                        "expired",
                        "approval_expired",
                        now,
                        reason="Unused approval expired; no execution was started.",
                    )
                    return self._run_state(conn, owner, run_id)[2]
            return public

    def recover(self) -> int:
        """Invalidate unfinished work at startup; return the number of changed runs.

        The caller must quiesce any executor first.  This method does not resume,
        retry, or claim that a side effect was undone.  Repeated recovery is a no-op.
        """
        with self._transaction() as conn:
            refs = conn.execute("""SELECT owner,id FROM g1_runs
                                WHERE status IN ('preparing','pending','approved','executing')
                                ORDER BY id""").fetchall()
            if not refs:
                return 0
            now = self._now()
            for ref in refs:
                run, action, _ = self._run_state(conn, ref["owner"], ref["id"])
                if run["status"] == "executing":
                    state = "uncertain"
                    reason = "Execution may have occurred before restart; replay is forbidden."
                else:
                    state = "interrupted"
                    reason = "Interrupted before execution at startup; approval invalidated."
                self._transition(conn, run, action, state, "recovered_" + state, now, reason=reason)
            return len(refs)

    def events(self, owner: str, run_id: str) -> list[dict]:
        _text(owner, "owner")
        _text(run_id, "run_id")
        with self._transaction(write=False) as conn:
            self._run_state(conn, owner, run_id)
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT
                        id,run_id,action_id,event,from_status,to_status,timestamp
                        FROM g1_events WHERE owner=? AND run_id=? ORDER BY id""",
                    (owner, run_id),
                )
            ]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                try:
                    conn.close()
                except sqlite3.Error as exc:
                    raise LedgerError("Could not close the ledger connection.") from exc
