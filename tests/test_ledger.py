"""Real local SQLite contract tests using only synthetic data and stdlib tools.

Run with the project's interpreter and PYTHONPATH=src.  No provider, microphone,
network, package installation, account, or personal environment is accessed.
"""

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest import mock

from workbench_pilot.ledger import Conflict, Ledger, LedgerError, NotFound, StaleApproval

OWNER = "synthetic-owner-a"
OTHER = "synthetic-owner-b"


class FakeClock:
    def __init__(self, value=1_700_000_000.0):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="g1-synthetic-ledger-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pilot.sqlite3"
        self.clock = FakeClock()
        self.ledger = Ledger(self.path, clock=self.clock)
        self.addCleanup(lambda: self.ledger.close())

    def run_row(self, request_id=None, owner=OWNER, payload=None):
        if payload is None:
            payload = {"fixture_id": "draftbox", "text": "Synthetic draft only"}
        return self.ledger.create_run(owner, request_id or uuid.uuid4().hex, payload)[0]

    @staticmethod
    def binding(run_id):
        return {
            "origin": "http://127.0.0.1:8123",
            "tab_id": run_id,
            "account": "synthetic-account-a",
            "recipient": "synthetic-recipient-a",
            "page_epoch": "synthetic-page-1",
            "surface": "textarea",
            "before_text": "",
            "save_count": 0,
            "facts": {"tone": {"revision": 1, "value": "concise"}},
        }

    def pending(self, *, ttl=300, owner=OWNER, request_id=None):
        run = self.run_row(request_id, owner)
        binding = self.binding(run["id"])
        action = self.ledger.propose(
            run["id"], "save_draft", {"text": "Synthetic draft only"}, binding, ttl_seconds=ttl
        )
        return run, action, binding

    def approved(self, *, ttl=300):
        run, action, binding = self.pending(ttl=ttl)
        action = self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        return run, action, binding

    def executing(self):
        run, action, binding = self.approved()
        action = self.ledger.begin(OWNER, action["id"], current_binding=binding)
        return run, action, binding

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.path, clock=self.clock)

    def sql(self, statement, parameters=()):
        with closing(sqlite3.connect(str(self.path))) as conn:
            result = conn.execute(statement, parameters).fetchall()
            conn.commit()
            return result

    def race(self, functions):
        barrier = threading.Barrier(len(functions))

        def attempt(function):
            barrier.wait(timeout=15)
            try:
                return function()
            except LedgerError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(attempt, function) for function in functions]
            return [future.result(timeout=25) for future in futures]

    def assert_state(self, run_id, action_id, expected):
        self.assertEqual(self.ledger.get_run(OWNER, run_id)["status"], expected)
        if action_id is not None:
            self.assertEqual(self.ledger.get_action(OWNER, action_id)["status"], expected)

    def assert_unusable(self, action, binding):
        with self.assertRaises(StaleApproval):
            self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        with self.assertRaises(StaleApproval):
            self.ledger.begin(OWNER, action["id"], current_binding=binding)


class FactTests(LedgerCase):
    def test_schema_uses_wal_foreign_keys_and_full_sync(self):
        self.assertEqual(self.ledger._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.ledger._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.ledger._conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])
        self.assertIn(self.ledger.search_backend, {"fts5", "like"})

    def test_seed_is_insert_only_and_scoped(self):
        self.ledger.seed_facts(OWNER, {"tone": "concise", "place": "Synthetic City"})
        self.ledger.seed_facts(OTHER, {"tone": "verbose"})
        self.ledger.seed_facts(OWNER, {"tone": "should not replace", "new": "fixture"})
        self.assertEqual(
            self.ledger.get_fact(OWNER, "tone"),
            {
                "key": "tone",
                "value": "concise",
                "revision": 1,
                "deleted": False,
                "source": "synthetic_fixture",
            },
        )
        self.assertEqual(self.ledger.get_fact(OTHER, "tone")["value"], "verbose")
        self.assertEqual([f["key"] for f in self.ledger.list_facts(OWNER)], ["new", "place", "tone"])
        self.assertIsNone(self.ledger.get_fact(OTHER, "place"))
        self.assertEqual(self.ledger.list_facts("synthetic-absent-owner"), [])
        self.assertEqual(self.sql("SELECT count(*) FROM g1_fact_revisions")[0][0], 4)

    def test_revision_cas_and_immutable_history(self):
        fact = self.ledger.set_fact(OWNER, "tone", "concise", expected_revision=0)
        self.assertEqual(fact["revision"], 1)
        fact = self.ledger.set_fact(OWNER, "tone", "warm", expected_revision=1)
        self.assertEqual(fact["revision"], 2)
        with self.assertRaises(Conflict):
            self.ledger.set_fact(OWNER, "tone", "stale", expected_revision=1)
        with self.assertRaises(Conflict):
            self.ledger.set_fact(OWNER, "missing", "stale", expected_revision=1)
        same = self.ledger.set_fact(OWNER, "tone", "warm", expected_revision=2)
        self.assertEqual(same["revision"], 3)
        history = self.sql(
            "SELECT revision,value,deleted FROM g1_fact_revisions WHERE owner=? AND key=? ORDER BY revision",
            (OWNER, "tone"),
        )
        self.assertEqual(history, [(1, "concise", 0), (2, "warm", 0), (3, "warm", 0)])

    def test_deletion_tombstone_no_bootstrap_resurrection_and_explicit_new_revision(self):
        self.ledger.seed_facts(OWNER, {"draft": "stale keywordalpha"})
        self.ledger.seed_facts(OTHER, {"draft": "other keywordalpha"})
        tombstone = self.ledger.delete_fact(OWNER, "draft", expected_revision=1)
        self.assertEqual(tombstone["revision"], 2)
        self.assertTrue(tombstone["deleted"])
        self.assertIsNone(tombstone["value"])
        self.assertIsNone(self.ledger.get_fact(OWNER, "draft"))
        self.assertEqual(self.ledger.list_facts(OWNER), [])
        self.assertEqual(self.ledger.search_facts(OWNER, "keywordalpha"), [])
        self.assertEqual(len(self.ledger.search_facts(OTHER, "keywordalpha")), 1)
        self.ledger.seed_facts(OWNER, {"draft": "stale keywordalpha"})
        self.assertIsNone(self.ledger.get_fact(OWNER, "draft"))
        with self.assertRaises(Conflict):
            self.ledger.set_fact(OWNER, "draft", "old value", expected_revision=1)
        with self.assertRaises(Conflict):
            self.ledger.delete_fact(OWNER, "draft", expected_revision=1)
        self.assertEqual(self.ledger.delete_fact(OWNER, "draft", expected_revision=2), tombstone)
        self.reopen()
        self.assertIsNone(self.ledger.get_fact(OWNER, "draft"))
        restored = self.ledger.set_fact(OWNER, "draft", "fresh keywordbeta", expected_revision=2)
        self.assertEqual(restored["revision"], 3)
        self.assertFalse(restored["deleted"])
        self.assertEqual(self.ledger.search_facts(OWNER, "keywordalpha"), [])
        self.assertEqual(self.ledger.search_facts(OWNER, "keywordbeta"), [restored])
        self.assertEqual(
            self.sql(
                "SELECT revision,value,deleted FROM g1_fact_revisions WHERE owner=? ORDER BY revision",
                (OWNER,),
            ),
            [(1, "stale keywordalpha", 0), (2, None, 1), (3, "fresh keywordbeta", 0)],
        )

    def test_delete_missing_fact_is_not_found(self):
        with self.assertRaises(NotFound):
            self.ledger.delete_fact(OWNER, "missing")

    def test_invalid_seed_is_atomic(self):
        for items in ({"valid": "fixture", "invalid": 42}, {"valid": "fixture", "": "bad"}, [], None):
            with self.subTest(items=repr(items)):
                with self.assertRaises(LedgerError):
                    self.ledger.seed_facts(OWNER, items)
                self.assertEqual(self.ledger.list_facts(OWNER), [])

    def test_fact_input_limits_and_types(self):
        for owner in (None, "", " \t", 1, False, "bad\x00owner", "\ud800"):
            with self.subTest(owner=repr(owner)), self.assertRaises(LedgerError):
                self.ledger.set_fact(owner, "key", "value")
        for key in (None, "", " ", 1, "k" * 121, "bad\x00key", "\udfff"):
            with self.subTest(key=repr(key)), self.assertRaises(LedgerError):
                self.ledger.set_fact(OWNER, key, "value")
        for value in (None, 1, [], {}, "v" * 4001, "bad\x00value", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(LedgerError):
                self.ledger.set_fact(OWNER, "key", value)
        for revision in (True, False, -1, 1.0, "1", {}, []):
            with self.subTest(revision=repr(revision)), self.assertRaises(LedgerError):
                self.ledger.set_fact(OWNER, "key", "value", expected_revision=revision)
        boundary = self.ledger.set_fact(OWNER, "k" * 120, "v" * 4000)
        self.assertEqual(len(boundary["value"]), 4000)
        self.assertEqual(self.ledger.set_fact(OWNER, "empty", "")["value"], "")

    def test_search_is_bounded_literal_and_tenant_scoped(self):
        self.ledger.seed_facts(OWNER, {f"topic-{i:03d}": "synthetic common keyword" for i in range(120)})
        self.ledger.seed_facts(OTHER, {"private": "synthetic common foreignonly"})
        self.assertEqual(len(self.ledger.search_facts(OWNER, "common")), 10)
        self.assertEqual(len(self.ledger.search_facts(OWNER, "common", 3)), 3)
        self.assertEqual(len(self.ledger.search_facts(OWNER, "common", 100)), 100)
        self.assertEqual(self.ledger.search_facts(OWNER, "foreignonly"), [])
        self.assertEqual(self.ledger.search_facts(OWNER, "common missingkeyword"), [])
        self.assertEqual(self.ledger.search_facts(OWNER, ""), [])
        self.assertEqual(self.ledger.search_facts(OWNER, '% * : " -'), [])
        self.assertEqual(self.ledger.search_facts(OWNER, "common OR foreignonly"), [])
        self.assertEqual(self.ledger.search_facts("synthetic-owner-a' OR 1=1 --", "common"), [])
        self.assertEqual(len(self.ledger.search_facts(OWNER, '"common"')), 10)
        for limit in (None, True, False, 0, -1, 101, 1.5, "10"):
            with self.subTest(limit=limit), self.assertRaises(LedgerError):
                self.ledger.search_facts(OWNER, "common", limit)
        for query in (None, 1, {}, "x" * 1001, " ".join(f"word{i}" for i in range(33))):
            with self.subTest(query=repr(query)), self.assertRaises(LedgerError):
                self.ledger.search_facts(OWNER, query)

    def test_fts_unavailable_explicit_same_owner_like_fallback(self):
        real_connect = sqlite3.connect

        class NoFTSConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    raise sqlite3.OperationalError("no such module: fts5")
                return super().execute(sql, parameters)

        def connect(*args, **kwargs):
            return real_connect(*args, factory=NoFTSConnection, **kwargs)

        with mock.patch("workbench_pilot.ledger.sqlite3.connect", side_effect=connect):
            with self.assertLogs("workbench_pilot.ledger", level="WARNING") as logs:
                fallback = Ledger(Path(self.tmp.name) / "fallback.sqlite3", clock=self.clock)
        self.addCleanup(fallback.close)
        self.assertIn("same-owner LIKE", logs.output[0])
        self.assertEqual(fallback.search_backend, "like")
        fallback.seed_facts(OWNER, {"one": "item_1 common", "two": "itemX1 common", "old": "deletedonly"})
        fallback.seed_facts(OTHER, {"foreign": "item_1 common foreignonly"})
        self.assertEqual([f["key"] for f in fallback.search_facts(OWNER, "item_1")], ["one"])
        self.assertEqual(fallback.search_facts(OWNER, "foreignonly"), [])
        self.assertEqual(len(fallback.search_facts(OWNER, "common", 1)), 1)
        fallback.delete_fact(OWNER, "old")
        self.assertEqual(fallback.search_facts(OWNER, "deletedonly"), [])
        self.assertEqual(fallback.search_facts(OWNER, "%"), [])
        fallback.close()
        rebuilt = Ledger(Path(self.tmp.name) / "fallback.sqlite3", clock=self.clock)
        self.addCleanup(rebuilt.close)
        self.assertEqual(rebuilt.search_facts(OWNER, "deletedonly"), [])
        self.assertEqual(rebuilt.search_facts(OWNER, "foreignonly"), [])
        self.assertEqual(len(rebuilt.search_facts(OWNER, "common")), 2)

    def test_thread_race_fact_creation_and_revision_updates(self):
        results = self.race(
            [
                lambda i=i: self.ledger.set_fact(OWNER, "race", f"synthetic-{i}", expected_revision=0)
                for i in range(12)
            ]
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, Conflict) for r in results), 11)
        results = self.race(
            [
                lambda i=i: self.ledger.set_fact(OWNER, "race", f"updated-{i}", expected_revision=1)
                for i in range(12)
            ]
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(self.ledger.get_fact(OWNER, "race")["revision"], 2)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_fact_revisions")[0][0], 2)

    def test_distinct_connections_serialize_fact_cas(self):
        other = Ledger(self.path, clock=self.clock)
        self.addCleanup(other.close)
        self.ledger.set_fact(OWNER, "race", "initial")
        results = self.race(
            [
                lambda: self.ledger.set_fact(OWNER, "race", "first", expected_revision=1),
                lambda: other.set_fact(OWNER, "race", "second", expected_revision=1),
            ]
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, Conflict) for r in results), 1)
        self.assertEqual(self.ledger.get_fact(OWNER, "race"), other.get_fact(OWNER, "race"))

    def test_facts_survive_restart_without_reseed(self):
        self.ledger.seed_facts(OWNER, {"kept": "local fixture", "removed": "old fixture"})
        self.ledger.set_fact(OWNER, "kept", "new fixture", expected_revision=1)
        self.ledger.delete_fact(OWNER, "removed")
        self.reopen()
        self.assertEqual(self.ledger.recover(), 0)
        self.assertEqual(self.ledger.get_fact(OWNER, "kept")["revision"], 2)
        self.assertIsNone(self.ledger.get_fact(OWNER, "removed"))
        self.assertEqual([f["key"] for f in self.ledger.search_facts(OWNER, "fixture")], ["kept"])


class RunTests(LedgerCase):
    def test_create_run_fields_and_canonical_request_deduplication(self):
        payload = {"fixture": "draftbox", "nested": {"a": 1, "b": [False, None, "é"]}}
        run, created = self.ledger.create_run(OWNER, "synthetic-request-1", payload)
        self.assertTrue(created)
        self.assertEqual(uuid.UUID(hex=run["id"]).hex, run["id"])
        self.assertEqual(
            set(run),
            {
                "id",
                "owner",
                "request_id",
                "status",
                "action_id",
                "proposal",
                "receipt",
                "reason",
                "cancel_requested",
            },
        )
        self.assertEqual(run["status"], "preparing")
        self.assertFalse(run["cancel_requested"])
        self.assertIsNone(run["proposal"])
        self.assertIsNone(run["receipt"])
        duplicate, created = self.ledger.create_run(
            OWNER,
            "synthetic-request-1",
            {
                "nested": {"b": [False, None, "é"], "a": 1},
                "fixture": "draftbox",
            },
        )
        self.assertFalse(created)
        self.assertEqual(duplicate, run)
        for changed in (
            {"fixture": "other"},
            {"fixture": "draftbox", "nested": {"a": True, "b": [False, None, "é"]}},
            {"fixture": "draftbox", "nested": {"a": 1.0, "b": [False, None, "é"]}},
        ):
            with self.subTest(changed=changed), self.assertRaises(Conflict):
                self.ledger.create_run(OWNER, "synthetic-request-1", changed)
        other, created = self.ledger.create_run(OTHER, "synthetic-request-1", payload)
        self.assertTrue(created)
        self.assertNotEqual(other["id"], run["id"])
        self.assertEqual(len(self.ledger.events(OWNER, run["id"])), 1)

    def test_proposal_digest_is_canonical_immutable_and_bound(self):
        run = self.run_row()
        args = {"z": 2, "text": "Synthetic café"}
        binding = self.binding(run["id"])
        action = self.ledger.propose(run["id"], "save_draft", args, binding, ttl_seconds=12)
        canonical = json.dumps(
            {"kind": "save_draft", "args": args, "binding": binding},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertEqual(action["digest"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(action["expires_at"], self.clock() + 12)
        self.assertEqual(action["status"], "pending")
        self.assertEqual(action["owner"], OWNER)
        self.assertEqual(action["run_id"], run["id"])
        self.assertEqual(uuid.UUID(hex=action["id"]).hex, action["id"])
        args["text"] = "changed caller object"
        binding["facts"]["tone"]["revision"] = 999
        action["args"]["text"] = "changed returned object"
        fresh = self.ledger.get_action(OWNER, action["id"])
        self.assertEqual(fresh["args"]["text"], "Synthetic café")
        self.assertEqual(fresh["binding"]["facts"]["tone"]["revision"], 1)
        with self.assertRaises(Conflict):
            self.ledger.propose(run["id"], "save_draft", {}, {})
        self.assertEqual(self.ledger.get_run(OWNER, run["id"])["proposal"], fresh)

    def test_approval_and_begin_are_one_shot_even_after_completion(self):
        run, action, binding = self.pending()
        self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        self.assert_state(run["id"], action["id"], "approved")
        for approved in (True, False):
            with self.assertRaises(StaleApproval):
                self.ledger.decide(OWNER, action["id"], approved, digest=action["digest"])
        started = self.ledger.begin(
            OWNER, action["id"], current_binding=dict(reversed(list(binding.items())))
        )
        self.assertEqual(started["status"], "executing")
        self.assert_unusable(action, binding)
        receipt = {"verified": True, "content": "Synthetic draft", "save_count": 1}
        completed = self.ledger.complete(OWNER, action["id"], receipt)
        self.assertEqual(completed["status"], "completed")
        receipt["content"] = "caller mutation"
        self.assertEqual(self.ledger.get_run(OWNER, run["id"])["receipt"]["content"], "Synthetic draft")
        with self.assertRaises(Conflict):
            self.ledger.complete(OWNER, action["id"], {"save_count": 2})
        self.assert_unusable(action, binding)
        self.assertEqual(self.ledger.get_run(OWNER, run["id"])["receipt"]["save_count"], 1)

    def test_denial_is_terminal_and_cannot_execute(self):
        run, action, binding = self.pending()
        denied = self.ledger.decide(OWNER, action["id"], False, digest=action["digest"])
        self.assertEqual(denied["status"], "denied")
        self.assert_state(run["id"], action["id"], "denied")
        self.assert_unusable(action, binding)
        with self.assertRaises(Conflict):
            self.ledger.complete(OWNER, action["id"], {})
        before = self.ledger.get_run(OWNER, run["id"])
        self.assertEqual(self.ledger.cancel(OWNER, run["id"]), before)
        self.assertEqual(self.ledger.fail(OWNER, run["id"], "late synthetic error"), before)

    def test_pending_cannot_begin_or_complete(self):
        run, action, binding = self.pending()
        with self.assertRaises(StaleApproval):
            self.ledger.begin(OWNER, action["id"], current_binding=binding)
        with self.assertRaises(Conflict):
            self.ledger.complete(OWNER, action["id"], {})
        self.assert_state(run["id"], action["id"], "pending")

    def test_wrong_digest_commits_stale_before_raising(self):
        run, action, binding = self.pending()
        bad_digest = "0" * 64 if action["digest"] != "0" * 64 else "1" * 64
        with self.assertRaises(StaleApproval):
            self.ledger.decide(OWNER, action["id"], True, digest=bad_digest)
        self.assert_state(run["id"], action["id"], "stale")
        self.assertEqual(self.ledger.events(OWNER, run["id"])[-1]["event"], "approval_stale")
        self.reopen()
        self.assertEqual(self.ledger.recover(), 0)
        self.assert_state(run["id"], action["id"], "stale")
        self.assert_unusable(action, binding)

    def test_changed_binding_fields_commit_stale_and_never_rearm(self):
        alterations = {
            "origin": "http://127.0.0.1:8124",
            "tab_id": "synthetic-other-tab",
            "recipient": "synthetic-recipient-b",
            "account": "synthetic-account-b",
            "page_epoch": "synthetic-page-2",
            "surface": "iframe",
            "before_text": "changed draft",
            "save_count": 1,
            "facts": {"tone": {"revision": 2, "value": "warm"}},
        }
        for key, value in alterations.items():
            with self.subTest(key=key):
                run, action, binding = self.approved()
                changed = copy.deepcopy(binding)
                changed[key] = value
                with self.assertRaises(StaleApproval):
                    self.ledger.begin(OWNER, action["id"], current_binding=changed)
                self.assert_state(run["id"], action["id"], "stale")
                self.assert_unusable(action, binding)
        self.reopen()
        self.assertEqual(self.ledger.recover(), 0)
        self.assert_state(run["id"], action["id"], "stale")

    def test_binding_uses_canonical_json_not_python_loose_equality(self):
        for substituted in (False, 0.0, "0"):
            with self.subTest(value=substituted):
                run, action, binding = self.approved()
                changed = copy.deepcopy(binding)
                changed["save_count"] = substituted
                with self.assertRaises(StaleApproval):
                    self.ledger.begin(OWNER, action["id"], current_binding=changed)
                self.assert_state(run["id"], action["id"], "stale")

    def test_expiry_at_decision_boundary_is_persisted_despite_error(self):
        run, action, binding = self.pending(ttl=10)
        self.clock.advance(10)
        with self.assertRaises(StaleApproval):
            self.ledger.decide(OWNER, action["id"], False, digest=action["digest"])
        self.assert_state(run["id"], action["id"], "expired")
        self.reopen()
        self.assertEqual(self.ledger.recover(), 0)
        self.clock.advance(-100)
        self.assert_unusable(action, binding)
        self.assertEqual(self.ledger.events(OWNER, run["id"])[-1]["event"], "approval_expired")

    def test_expiry_is_rechecked_at_atomic_begin_and_persisted(self):
        run, action, binding = self.approved(ttl=1)
        self.clock.advance(1)
        with self.assertRaises(StaleApproval):
            self.ledger.begin(OWNER, action["id"], current_binding=binding)
        self.assert_state(run["id"], action["id"], "expired")
        self.reopen()
        self.assert_state(run["id"], action["id"], "expired")
        self.assert_unusable(action, binding)

    def test_completion_after_expiry_is_truthful_once_execution_began(self):
        run, action, binding = self.approved(ttl=1)
        self.ledger.begin(OWNER, action["id"], current_binding=binding)
        self.clock.advance(600)
        self.ledger.complete(OWNER, action["id"], {"verified": True, "save_count": 1})
        self.assert_state(run["id"], action["id"], "completed")

    def test_wrong_owner_cannot_read_decide_execute_complete_cancel_fail_or_read_events(self):
        run, action, binding = self.approved()
        operations = [
            lambda: self.ledger.get_run(OTHER, run["id"]),
            lambda: self.ledger.get_action(OTHER, action["id"]),
            lambda: self.ledger.decide(OTHER, action["id"], True, digest=action["digest"]),
            lambda: self.ledger.begin(OTHER, action["id"], current_binding=binding),
            lambda: self.ledger.complete(OTHER, action["id"], {}),
            lambda: self.ledger.cancel(OTHER, run["id"]),
            lambda: self.ledger.fail(OTHER, run["id"], "synthetic error"),
            lambda: self.ledger.events(OTHER, run["id"]),
        ]
        for operation in operations:
            with self.assertRaises(NotFound):
                operation()
        self.assert_state(run["id"], action["id"], "approved")
        self.assertEqual(len(self.ledger.events(OWNER, run["id"])), 3)

    def test_missing_ids_are_not_found_and_missing_identity_is_invalid(self):
        for operation in (
            lambda: self.ledger.get_run(OWNER, "missing"),
            lambda: self.ledger.get_action(OWNER, "missing"),
            lambda: self.ledger.propose("missing", "save_draft", {}, {}),
            lambda: self.ledger.cancel(OWNER, "missing"),
            lambda: self.ledger.events(OWNER, "missing"),
            lambda: self.ledger.begin(OWNER, "missing", current_binding={}),
            lambda: self.ledger.decide(OWNER, "missing", True, digest="0" * 64),
            lambda: self.ledger.complete(OWNER, "missing", {}),
            lambda: self.ledger.fail(OWNER, "missing", "synthetic error"),
        ):
            with self.assertRaises(NotFound):
                operation()
        for invalid in (None, "", " ", 42, False):
            with self.subTest(invalid=invalid):
                for operation in (
                    lambda invalid=invalid: self.ledger.get_run(OWNER, invalid),
                    lambda invalid=invalid: self.ledger.get_action(OWNER, invalid),
                    lambda invalid=invalid: self.ledger.get_run(invalid, "missing"),
                    lambda invalid=invalid: self.ledger.create_run(invalid, "request", {}),
                ):
                    with self.assertRaises(LedgerError):
                        operation()

    def test_cancel_before_execution_is_terminal_at_each_stage(self):
        for stage in ("preparing", "pending", "approved"):
            with self.subTest(stage=stage):
                if stage == "preparing":
                    run, action, binding = self.run_row(), None, None
                elif stage == "pending":
                    run, action, binding = self.pending()
                else:
                    run, action, binding = self.approved()
                cancelled = self.ledger.cancel(OWNER, run["id"])
                self.assertTrue(cancelled["cancel_requested"])
                self.assertEqual(cancelled["status"], "cancelled")
                count = len(self.ledger.events(OWNER, run["id"]))
                self.assertEqual(self.ledger.cancel(OWNER, run["id"]), cancelled)
                self.assertEqual(len(self.ledger.events(OWNER, run["id"])), count)
                with self.assertRaises(Conflict):
                    self.ledger.propose(run["id"], "save_draft", {}, {})
                if action is not None:
                    self.assert_state(run["id"], action["id"], "cancelled")
                    self.assert_unusable(action, binding)

    def test_cancel_during_execution_does_not_erase_completed_evidence(self):
        run, action, binding = self.executing()
        requested = self.ledger.cancel(OWNER, run["id"])
        self.assertTrue(requested["cancel_requested"])
        self.assertEqual(requested["status"], "executing")
        self.assertIsNone(requested["receipt"])
        self.assertEqual(self.ledger.cancel(OWNER, run["id"]), requested)
        receipt = {"verified": True, "save_count": 1, "content": "synthetic saved draft"}
        self.ledger.complete(OWNER, action["id"], receipt)
        completed = self.ledger.get_run(OWNER, run["id"])
        self.assertEqual(completed["receipt"], receipt)
        self.assertTrue(completed["cancel_requested"])
        count = len(self.ledger.events(OWNER, run["id"]))
        self.assertEqual(self.ledger.cancel(OWNER, run["id"]), completed)
        self.assertEqual(self.ledger.fail(OWNER, run["id"], "late error", uncertain=True), completed)
        self.assertEqual(len(self.ledger.events(OWNER, run["id"])), count)
        self.reopen()
        self.assertEqual(self.ledger.recover(), 0)
        self.assertEqual(self.ledger.get_run(OWNER, run["id"]), completed)
        self.assert_unusable(action, binding)

    def test_fail_and_uncertain_are_terminal_from_each_live_stage(self):
        for stage in ("preparing", "pending", "approved", "executing"):
            for uncertain in (False, True):
                with self.subTest(stage=stage, uncertain=uncertain):
                    if stage == "preparing":
                        run, action, binding = self.run_row(), None, None
                    else:
                        factory = {
                            "pending": self.pending,
                            "approved": self.approved,
                            "executing": self.executing,
                        }[stage]
                        run, action, binding = factory()
                    failed = self.ledger.fail(
                        OWNER, run["id"], "synthetic failure marker", uncertain=uncertain
                    )
                    self.assertEqual(failed["status"], "uncertain" if uncertain else "failed")
                    self.assertEqual(failed["reason"], "synthetic failure marker")
                    self.assertIsNone(failed["receipt"])
                    self.assertNotIn(
                        "synthetic failure marker", json.dumps(self.ledger.events(OWNER, run["id"]))
                    )
                    self.assertEqual(
                        self.ledger.fail(OWNER, run["id"], "another error", uncertain=not uncertain), failed
                    )
                    self.assertEqual(self.ledger.cancel(OWNER, run["id"]), failed)
                    if action:
                        self.assert_unusable(action, binding)

    def test_duplicate_request_after_completed_or_cancelled_never_creates_new_work(self):
        for terminal in ("completed", "cancelled"):
            with self.subTest(terminal=terminal):
                request_id = "synthetic-request-" + terminal
                run, action, binding = self.pending(request_id=request_id)
                if terminal == "completed":
                    self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
                    self.ledger.begin(OWNER, action["id"], current_binding=binding)
                    self.ledger.complete(OWNER, action["id"], {"verified": True})
                else:
                    self.ledger.cancel(OWNER, run["id"])
                duplicate, created = self.ledger.create_run(
                    OWNER,
                    request_id,
                    {
                        "text": "Synthetic draft only",
                        "fixture_id": "draftbox",
                    },
                )
                self.assertFalse(created)
                self.assertEqual(duplicate["id"], run["id"])
                self.assertEqual(duplicate["status"], terminal)
                with self.assertRaises(Conflict):
                    self.ledger.create_run(OWNER, request_id, {"fixture_id": "changed"})
                with self.assertRaises(Conflict):
                    self.ledger.propose(duplicate["id"], "save_draft", {}, {})

    def test_events_have_durable_transitions_timestamps_and_no_free_form_values(self):
        run = self.run_row(payload={"marker": "synthetic-payload-marker"})
        self.clock.advance(1)
        action = self.ledger.propose(
            run["id"],
            "save_draft",
            {"text": "synthetic-argument-marker"},
            {"context": "synthetic-context-marker"},
        )
        self.clock.advance(1)
        self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        self.clock.advance(1)
        self.ledger.begin(OWNER, action["id"], current_binding=action["binding"])
        self.clock.advance(1)
        self.ledger.complete(OWNER, action["id"], {"content": "synthetic-receipt-marker"})
        events = self.ledger.events(OWNER, run["id"])
        self.assertEqual(
            [e["event"] for e in events],
            ["run_created", "action_proposed", "approval_granted", "action_started", "action_completed"],
        )
        self.assertEqual(
            [e["from_status"] for e in events], [None, "preparing", "pending", "approved", "executing"]
        )
        self.assertEqual(
            [e["to_status"] for e in events], ["preparing", "pending", "approved", "executing", "completed"]
        )
        self.assertEqual([e["timestamp"] for e in events], [1_700_000_000.0 + i for i in range(5)])
        self.assertEqual([e["id"] for e in events], sorted({e["id"] for e in events}))
        self.assertIsNone(events[0]["action_id"])
        self.assertTrue(all(e["action_id"] == action["id"] for e in events[1:]))
        for marker in (
            "synthetic-payload-marker",
            "synthetic-argument-marker",
            "synthetic-context-marker",
            "synthetic-receipt-marker",
        ):
            self.assertNotIn(marker, json.dumps(events))
        self.reopen()
        self.assertEqual(self.ledger.events(OWNER, run["id"]), events)

    def test_transaction_error_rolls_back_action_run_and_event_together(self):
        run, action, _ = self.pending()
        prior_events = self.ledger.events(OWNER, run["id"])
        with mock.patch.object(
            self.ledger, "_event", side_effect=sqlite3.OperationalError("synthetic disk failure")
        ):
            with self.assertRaises(LedgerError):
                self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        self.assert_state(run["id"], action["id"], "pending")
        self.assertEqual(self.ledger.events(OWNER, run["id"]), prior_events)
        self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        self.assert_state(run["id"], action["id"], "approved")

    def test_failed_receipt_transaction_leaves_execution_claim_for_uncertain_handling(self):
        run, action, binding = self.executing()
        before = self.ledger.events(OWNER, run["id"])
        with mock.patch.object(
            self.ledger, "_event", side_effect=sqlite3.OperationalError("synthetic receipt failure")
        ):
            with self.assertRaises(LedgerError):
                self.ledger.complete(OWNER, action["id"], {"verified": True, "save_count": 1})
        self.assert_state(run["id"], action["id"], "executing")
        self.assertIsNone(self.ledger.get_run(OWNER, run["id"])["receipt"])
        self.assertEqual(self.ledger.events(OWNER, run["id"]), before)
        self.ledger.fail(OWNER, run["id"], "Synthetic possible write without durable receipt", uncertain=True)
        self.assert_state(run["id"], action["id"], "uncertain")
        self.assert_unusable(action, binding)

    def test_proposal_failure_does_not_leave_orphan_action(self):
        run = self.run_row()
        with mock.patch.object(
            self.ledger, "_event", side_effect=sqlite3.OperationalError("synthetic event failure")
        ):
            with self.assertRaises(LedgerError):
                self.ledger.propose(run["id"], "save_draft", {}, {})
        self.assertEqual(self.ledger.get_run(OWNER, run["id"])["status"], "preparing")
        self.assertEqual(self.sql("SELECT count(*) FROM g1_actions")[0][0], 0)
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])
        self.ledger.propose(run["id"], "save_draft", {}, {})


class InputAndCorruptionTests(LedgerCase):
    def test_invalid_json_values_are_rejected_before_writing(self):
        cycle = {}
        cycle["self"] = cycle
        deep = {}
        cursor = deep
        for _ in range(70):
            cursor["next"] = {}
            cursor = cursor["next"]
        invalid = [
            None,
            [],
            "object",
            {1: "bad key"},
            {"nested": {False: "bad key"}},
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            {"value": (1, 2)},
            {"value": {1, 2}},
            {"value": b"bytes"},
            {"value": object()},
            {"value": "\ud800"},
            {"value": "x" * 32769},
            {"value": "界" * 11000},
            cycle,
            deep,
        ]
        for index, payload in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(LedgerError):
                self.ledger.create_run(OWNER, f"invalid-{index}", payload)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_runs")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_events")[0][0], 0)

    def test_request_id_limit_and_invalid_types(self):
        for request_id in (None, 1, True, "", " ", "r" * 101, "bad\x00id"):
            with self.subTest(request_id=repr(request_id)), self.assertRaises(LedgerError):
                self.ledger.create_run(OWNER, request_id, {})
        self.assertTrue(self.ledger.create_run(OWNER, "r" * 100, {})[1])

    def test_invalid_proposal_or_ttl_leaves_run_preparing(self):
        run = self.run_row()
        for ttl in (None, True, 0, -1, "300", float("nan"), float("inf"), 1e-100):
            with self.subTest(ttl=ttl), self.assertRaises(LedgerError):
                self.ledger.propose(run["id"], "save_draft", {}, {}, ttl_seconds=ttl)
        for kind, args, binding in (
            ("", {}, {}),
            (None, {}, {}),
            ("x" * 121, {}, {}),
            ("save_draft", [], {}),
            ("save_draft", {}, []),
            ("save_draft", {"value": "x" * 18000}, {"value": "y" * 18000}),
        ):
            with self.subTest(kind=kind), self.assertRaises(LedgerError):
                self.ledger.propose(run["id"], kind, args, binding)
        self.assertEqual(self.ledger.get_run(OWNER, run["id"])["status"], "preparing")
        self.assertEqual(self.sql("SELECT count(*) FROM g1_actions")[0][0], 0)

    def test_invalid_decision_is_not_truthiness_and_does_not_consume_approval(self):
        run, action, _ = self.pending()
        for approved in (1, 0, "true", "false", None, []):
            with self.subTest(approved=approved), self.assertRaises(LedgerError):
                self.ledger.decide(OWNER, action["id"], approved, digest=action["digest"])
        for digest in (None, 1, "", "x" * 64, "0" * 63, "0" * 65):
            with self.subTest(digest=digest), self.assertRaises(LedgerError):
                self.ledger.decide(OWNER, action["id"], True, digest=digest)
        self.assert_state(run["id"], action["id"], "pending")

    def test_invalid_binding_receipt_and_failure_inputs_leave_state_unchanged(self):
        run, action, binding = self.approved()
        for invalid in (None, [], {"v": float("nan")}):
            with self.assertRaises(LedgerError):
                self.ledger.begin(OWNER, action["id"], current_binding=invalid)
        self.assert_state(run["id"], action["id"], "approved")
        self.ledger.begin(OWNER, action["id"], current_binding=binding)
        for invalid in (None, [], {"v": float("nan")}, {"large": "界" * 11000}):
            with self.assertRaises(LedgerError):
                self.ledger.complete(OWNER, action["id"], invalid)
        for reason in (None, 1, "", " ", "x" * 4001):
            with self.assertRaises(LedgerError):
                self.ledger.fail(OWNER, run["id"], reason)
        for uncertain in (None, 1, "false"):
            with self.assertRaises(LedgerError):
                self.ledger.fail(OWNER, run["id"], "synthetic reason", uncertain=uncertain)
        self.assert_state(run["id"], action["id"], "executing")
        self.ledger.complete(OWNER, action["id"], {})

    def test_invalid_clock_and_path_are_clear_errors(self):
        path = Path(self.tmp.name) / "invalid-clock.sqlite3"
        for clock in (None, 1, lambda: None, lambda: True, lambda: float("nan"), lambda: float("inf")):
            with self.subTest(clock=clock), self.assertRaises(LedgerError):
                Ledger(path, clock=clock)
        self.assertFalse(path.exists())
        for invalid in (None, 1, [], {}):
            with self.assertRaises(LedgerError):
                Ledger(invalid)
        run, action, _ = self.pending()
        self.clock.value = float("nan")
        with self.assertRaises(LedgerError):
            self.ledger.decide(OWNER, action["id"], True, digest=action["digest"])
        self.assert_state(run["id"], action["id"], "pending")

    def test_unrelated_database_is_not_adopted_or_modified(self):
        path = Path(self.tmp.name) / "unrelated.sqlite3"
        with closing(sqlite3.connect(str(path))) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('synthetic unrelated fixture')")
            conn.commit()
        before = path.read_bytes()
        with self.assertRaises(LedgerError):
            Ledger(path, clock=self.clock)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(str(path))) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(
                conn.execute("SELECT value FROM unrelated").fetchone()[0], "synthetic unrelated fixture"
            )

    def test_corrupt_file_and_unsupported_schema_are_refused(self):
        path = Path(self.tmp.name) / "corrupt.sqlite3"
        path.write_bytes(b"synthetic non-SQLite bytes")
        with self.assertRaises(LedgerError):
            Ledger(path, clock=self.clock)
        self.ledger.close()
        self.sql("PRAGMA user_version=999")
        with self.assertRaises(LedgerError):
            Ledger(self.path, clock=self.clock)
        self.assertEqual(self.sql("PRAGMA user_version")[0][0], 999)

    def test_corrupt_or_changed_stored_proposal_cannot_execute(self):
        for raw in (
            "{broken",
            '{"args":{},"binding":{},"kind":"tampered"}',
            '{"kind":"save_draft","kind":"other"}',
        ):
            with self.subTest(raw=raw):
                run, action, binding = self.approved()
                self.sql("UPDATE g1_actions SET proposal_json=? WHERE id=?", (raw, action["id"]))
                for operation in (
                    lambda run=run: self.ledger.get_run(OWNER, run["id"]),
                    lambda action=action: self.ledger.get_action(OWNER, action["id"]),
                    lambda action=action, binding=binding: self.ledger.begin(
                        OWNER, action["id"], current_binding=binding
                    ),
                ):
                    with self.assertRaises(LedgerError):
                        operation()
                self.assertEqual(
                    self.sql("SELECT status FROM g1_actions WHERE id=?", (action["id"],))[0][0], "approved"
                )

    def test_corrupt_digest_types_fail_with_public_errors_without_execution(self):
        for stored in ("é" * 64, "not-a-digest", b"0" * 64, "0" * 64):
            with self.subTest(stored=repr(stored)):
                run, action, binding = self.approved()
                self.sql("UPDATE g1_actions SET digest=? WHERE id=?", (stored, action["id"]))
                with self.assertRaises(LedgerError):
                    self.ledger.begin(OWNER, action["id"], current_binding=binding)
                self.assertEqual(
                    self.sql("SELECT status FROM g1_actions WHERE id=?", (action["id"],))[0][0], "approved"
                )
                other = self.run_row()
                self.sql("UPDATE g1_runs SET payload_digest=? WHERE id=?", (stored, other["id"]))
                with self.assertRaises(LedgerError):
                    self.ledger.get_run(OWNER, other["id"])

    def test_corrupt_fact_storage_fails_instead_of_returning_wrong_types(self):
        self.ledger.set_fact(OWNER, "invalid-value", "synthetic")
        self.sql(
            "UPDATE g1_facts SET value=? WHERE owner=? AND key=?",
            (b"synthetic bytes", OWNER, "invalid-value"),
        )
        with self.assertRaises(LedgerError):
            self.ledger.get_fact(OWNER, "invalid-value")
        with self.assertRaises(LedgerError):
            self.ledger.list_facts(OWNER)
        self.ledger.set_fact(OWNER, "invalid-revision", "synthetic")
        self.sql("UPDATE g1_facts SET revision=1.5 WHERE owner=? AND key=?", (OWNER, "invalid-revision"))
        with self.assertRaises(LedgerError):
            self.ledger.set_fact(OWNER, "invalid-revision", "replacement")
        with self.assertRaises(LedgerError):
            self.ledger.seed_facts(OWNER, {"invalid-revision": "replacement"})

    def test_foreign_keys_reject_orphaning_a_run_action(self):
        run, action, _ = self.pending()
        with self.assertRaises(LedgerError):
            with self.ledger._transaction() as conn:
                conn.execute("DELETE FROM g1_runs WHERE id=?", (run["id"],))
        self.assert_state(run["id"], action["id"], "pending")
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_corrupt_payload_and_receipt_are_not_silently_decoded(self):
        run = self.run_row("synthetic-corrupt-payload")
        self.sql("UPDATE g1_runs SET payload_json='{}' WHERE id=?", (run["id"],))
        with self.assertRaises(LedgerError):
            self.ledger.create_run(OWNER, "synthetic-corrupt-payload", {})
        with self.assertRaises(LedgerError):
            self.ledger.get_run(OWNER, run["id"])
        completed, action, _ = self.executing()
        self.ledger.complete(OWNER, action["id"], {"verified": True})
        self.sql("UPDATE g1_runs SET receipt_json='[]' WHERE id=?", (completed["id"],))
        with self.assertRaises(LedgerError):
            self.ledger.get_run(OWNER, completed["id"])
        with self.assertRaises(LedgerError):
            self.ledger.complete(OWNER, action["id"], {"replacement": True})
        self.assertEqual(
            self.sql("SELECT status,receipt_json FROM g1_runs WHERE id=?", (completed["id"],))[0],
            ("completed", "[]"),
        )

    def test_close_is_idempotent_and_closed_ledger_refuses_work(self):
        self.ledger.close()
        self.ledger.close()
        for operation in (
            lambda: self.ledger.get_fact(OWNER, "key"),
            lambda: self.ledger.search_facts(OWNER, ""),
            lambda: self.ledger.create_run(OWNER, "request", {}),
            self.ledger.recover,
        ):
            with self.assertRaises(LedgerError):
                operation()


class ConcurrencyTests(LedgerCase):
    def test_identical_request_race_creates_exactly_one_run(self):
        results = self.race(
            [
                lambda: self.ledger.create_run(OWNER, "synthetic-race-request", {"fixture": "draftbox"})
                for _ in range(16)
            ]
        )
        self.assertTrue(all(isinstance(r, tuple) for r in results))
        self.assertEqual(sum(r[1] for r in results), 1)
        self.assertEqual(len({r[0]["id"] for r in results}), 1)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_runs")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_events")[0][0], 1)

    def test_distinct_connections_deduplicate_the_same_request(self):
        other = Ledger(self.path, clock=self.clock)
        self.addCleanup(other.close)
        results = self.race(
            [
                lambda: self.ledger.create_run(OWNER, "synthetic-cross-connection", {"fixture": "draftbox"}),
                lambda: other.create_run(OWNER, "synthetic-cross-connection", {"fixture": "draftbox"}),
            ]
        )
        self.assertTrue(all(isinstance(result, tuple) for result in results))
        self.assertEqual(sum(result[1] for result in results), 1)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(len(other.events(OWNER, results[0][0]["id"])), 1)

    def test_conflicting_request_race_never_rebinds_request_id(self):
        results = self.race(
            [lambda i=i: self.ledger.create_run(OWNER, "synthetic-mismatch", {"value": i}) for i in range(12)]
        )
        self.assertEqual(sum(isinstance(r, tuple) for r in results), 1)
        self.assertEqual(sum(isinstance(r, Conflict) for r in results), 11)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_runs")[0][0], 1)

    def test_concurrent_proposals_create_one_action(self):
        run = self.run_row()
        results = self.race(
            [lambda i=i: self.ledger.propose(run["id"], "save_draft", {"value": i}, {}) for i in range(12)]
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, Conflict) for r in results), 11)
        self.assertEqual(self.sql("SELECT count(*) FROM g1_actions")[0][0], 1)
        self.assertEqual(len(self.ledger.events(OWNER, run["id"])), 2)

    def test_duplicate_and_opposite_decision_race_has_one_winner(self):
        run, action, _ = self.pending()
        results = self.race(
            [
                lambda i=i: self.ledger.decide(OWNER, action["id"], bool(i % 2), digest=action["digest"])
                for i in range(16)
            ]
        )
        winners = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(isinstance(r, StaleApproval) for r in results), 15)
        self.assert_state(run["id"], action["id"], winners[0]["status"])
        self.assertEqual(len(self.ledger.events(OWNER, run["id"])), 3)

    def test_atomic_begin_has_exactly_one_execution_claim(self):
        run, action, binding = self.approved()
        results = self.race(
            [
                lambda action=action, binding=binding: self.ledger.begin(
                    OWNER, action["id"], current_binding=binding
                )
                for _ in range(16)
            ]
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, StaleApproval) for r in results), 15)
        self.assert_state(run["id"], action["id"], "executing")
        self.assertEqual(sum(e["event"] == "action_started" for e in self.ledger.events(OWNER, run["id"])), 1)

    def test_distinct_connections_do_not_bypass_atomic_begin(self):
        run, action, binding = self.approved()
        other = Ledger(self.path, clock=self.clock)
        self.addCleanup(other.close)
        results = self.race(
            [
                lambda action=action, binding=binding: self.ledger.begin(
                    OWNER, action["id"], current_binding=binding
                ),
                lambda: other.begin(OWNER, action["id"], current_binding=binding),
            ]
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, StaleApproval) for r in results), 1)
        self.assertEqual(other.get_run(OWNER, run["id"])["status"], "executing")

    def test_begin_cancel_race_never_claims_side_effect_was_undone(self):
        for _ in range(12):
            run, action, binding = self.approved()
            results = self.race(
                [
                    lambda action=action, binding=binding: self.ledger.begin(
                        OWNER, action["id"], current_binding=binding
                    ),
                    lambda run=run: self.ledger.cancel(OWNER, run["id"]),
                ]
            )
            final = self.ledger.get_run(OWNER, run["id"])
            self.assertTrue(final["cancel_requested"])
            if isinstance(results[0], dict):
                self.assertEqual(results[0]["status"], "executing")
                self.assertEqual(final["status"], "executing")
                self.ledger.complete(OWNER, action["id"], {"verified": True})
                self.assertTrue(self.ledger.get_run(OWNER, run["id"])["cancel_requested"])
            else:
                self.assertIsInstance(results[0], StaleApproval)
                self.assertEqual(final["status"], "cancelled")
                self.assertFalse(
                    any(e["event"] == "action_started" for e in self.ledger.events(OWNER, run["id"]))
                )

    def test_approve_cancel_race_cannot_leave_an_executable_approval(self):
        run, action, binding = self.pending()
        self.race(
            [
                lambda: self.ledger.decide(OWNER, action["id"], True, digest=action["digest"]),
                lambda run=run: self.ledger.cancel(OWNER, run["id"]),
            ]
        )
        self.assert_state(run["id"], action["id"], "cancelled")
        self.assert_unusable(action, binding)

    def test_concurrent_completion_preserves_first_committed_receipt(self):
        run, action, _ = self.executing()
        results = self.race(
            [
                lambda i=i: self.ledger.complete(
                    OWNER, action["id"], {"synthetic_receipt_id": i, "verified": True}
                )
                for i in range(12)
            ]
        )
        winning_indices = [i for i, result in enumerate(results) if isinstance(result, dict)]
        self.assertEqual(len(winning_indices), 1)
        self.assertEqual(sum(isinstance(r, Conflict) for r in results), 11)
        self.assertEqual(
            self.ledger.get_run(OWNER, run["id"])["receipt"]["synthetic_receipt_id"], winning_indices[0]
        )
        self.assertEqual(
            sum(e["event"] == "action_completed" for e in self.ledger.events(OWNER, run["id"])), 1
        )


class RecoveryTests(LedgerCase):
    def test_explicit_startup_recovery_invalidates_all_unfinished_work(self):
        preparing = self.run_row()
        pending = self.pending()
        approved = self.approved()
        executing = self.executing()
        self.ledger.cancel(OWNER, executing[0]["id"])
        completed = self.executing()
        receipt = {"verified": True, "content": "durable synthetic receipt", "save_count": 1}
        self.ledger.complete(OWNER, completed[1]["id"], receipt)
        denied = self.pending()
        self.ledger.decide(OWNER, denied[1]["id"], False, digest=denied[1]["digest"])
        cancelled = self.pending()
        self.ledger.cancel(OWNER, cancelled[0]["id"])
        stale = self.approved()
        with self.assertRaises(StaleApproval):
            self.ledger.begin(OWNER, stale[1]["id"], current_binding={})
        expired = self.pending(ttl=1)
        self.clock.advance(1)
        with self.assertRaises(StaleApproval):
            self.ledger.decide(OWNER, expired[1]["id"], True, digest=expired[1]["digest"])
        failed = self.pending()
        self.ledger.fail(OWNER, failed[0]["id"], "synthetic failure")
        uncertain = self.executing()
        self.ledger.fail(OWNER, uncertain[0]["id"], "synthetic unknown write", uncertain=True)
        self.ledger.seed_facts(OWNER, {"kept": "synthetic fact", "deleted": "old value"})
        self.ledger.delete_fact(OWNER, "deleted")
        terminal = [
            (completed, "completed"),
            (denied, "denied"),
            (cancelled, "cancelled"),
            (stale, "stale"),
            (expired, "expired"),
            (failed, "failed"),
            (uncertain, "uncertain"),
        ]
        before = {entry[0]["id"]: self.ledger.get_run(OWNER, entry[0]["id"]) for entry, _ in terminal}
        self.reopen()
        self.assertEqual(self.ledger.recover(), 4)
        self.assert_state(preparing["id"], None, "interrupted")
        for entry in (pending, approved):
            self.assert_state(entry[0]["id"], entry[1]["id"], "interrupted")
            self.assert_unusable(entry[1], entry[2])
        self.assert_state(executing[0]["id"], executing[1]["id"], "uncertain")
        self.assertTrue(self.ledger.get_run(OWNER, executing[0]["id"])["cancel_requested"])
        self.assert_unusable(executing[1], executing[2])
        with self.assertRaises(Conflict):
            self.ledger.complete(OWNER, executing[1]["id"], {"late": "unverified"})
        for entry, state in terminal:
            self.assert_state(entry[0]["id"], entry[1]["id"], state)
            self.assertEqual(self.ledger.get_run(OWNER, entry[0]["id"]), before[entry[0]["id"]])
        self.assertEqual(self.ledger.get_run(OWNER, completed[0]["id"])["receipt"], receipt)
        self.assertEqual(self.ledger.get_fact(OWNER, "kept")["value"], "synthetic fact")
        self.assertIsNone(self.ledger.get_fact(OWNER, "deleted"))
        self.assertEqual(self.ledger.recover(), 0)
        duplicate, created = self.ledger.create_run(
            OWNER, approved[0]["request_id"], {"fixture_id": "draftbox", "text": "Synthetic draft only"}
        )
        self.assertFalse(created)
        self.assertEqual(duplicate["status"], "interrupted")
        with self.assertRaises(Conflict):
            self.ledger.propose(duplicate["id"], "save_draft", {}, {})
        self.reopen()
        self.assertEqual(self.ledger.recover(), 0)
        self.assertEqual(self.ledger.events(OWNER, executing[0]["id"])[-1]["event"], "recovered_uncertain")

    def test_recovery_covers_every_tenant_without_leaking_owner_reads(self):
        a = self.pending(owner=OWNER)
        b = self.pending(owner=OTHER)
        self.reopen()
        self.assertEqual(self.ledger.recover(), 2)
        self.assertEqual(self.ledger.get_run(OWNER, a[0]["id"])["status"], "interrupted")
        self.assertEqual(self.ledger.get_run(OTHER, b[0]["id"])["status"], "interrupted")
        with self.assertRaises(NotFound):
            self.ledger.get_run(OWNER, b[0]["id"])

    def test_actual_process_exit_preserves_claim_or_completed_receipt_without_replay(self):
        script = textwrap.dedent("""
            import json, os, sys
            from pathlib import Path
            from workbench_pilot.ledger import Ledger
            ledger = Ledger(Path(sys.argv[1]), clock=lambda: 1700000000.0)
            run, _ = ledger.create_run(
                "synthetic-owner-a", "synthetic-crash-request", {"fixture": "draftbox"}
            )
            binding = {"account": "synthetic-account-a", "revision": 1}
            action = ledger.propose(run["id"], "save_draft", {"text": "synthetic"}, binding)
            ledger.decide("synthetic-owner-a", action["id"], True, digest=action["digest"])
            ledger.begin("synthetic-owner-a", action["id"], current_binding=binding)
            if sys.argv[2] == "completed":
                ledger.complete("synthetic-owner-a", action["id"], {"verified": True, "save_count": 1})
            print(json.dumps({
                "run": run["id"], "action": action["id"],
                "digest": action["digest"], "binding": binding
            }), flush=True)
            os._exit(0)  # Deliberately skip close/finalizers to exercise WAL durability.
        """)
        for state in ("executing", "completed"):
            with self.subTest(state=state):
                path = Path(self.tmp.name) / ("synthetic-crash-" + state + ".sqlite3")
                child = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(path), state],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=20,
                )
                ids = json.loads(child.stdout)
                reopened = Ledger(path, clock=self.clock)
                self.addCleanup(reopened.close)
                self.assertEqual(reopened.recover(), 1 if state == "executing" else 0)
                run = reopened.get_run(OWNER, ids["run"])
                self.assertEqual(run["status"], "uncertain" if state == "executing" else "completed")
                if state == "completed":
                    self.assertEqual(run["receipt"], {"verified": True, "save_count": 1})
                else:
                    self.assertIsNone(run["receipt"])
                    self.assertIn("may have occurred", run["reason"])
                with self.assertRaises(StaleApproval):
                    reopened.begin(OWNER, ids["action"], current_binding=ids["binding"])
                with self.assertRaises(StaleApproval):
                    reopened.decide(OWNER, ids["action"], True, digest=ids["digest"])
                duplicate, created = reopened.create_run(
                    OWNER, "synthetic-crash-request", {"fixture": "draftbox"}
                )
                self.assertFalse(created)
                self.assertEqual(duplicate, run)
                with self.assertRaises(Conflict):
                    reopened.create_run(OWNER, "synthetic-crash-request", {"fixture": "changed"})


if __name__ == "__main__":
    unittest.main()
