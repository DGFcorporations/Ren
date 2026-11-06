"""One deterministic offline workflow and one durable authority for every write."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from .adapters import SEED_FACTS, draft_from_fixture, integration_status, scripted_transcript
from .browser import FixtureBrowser, FixtureViolation
from .ledger import Conflict, Ledger, NotFound
from .policy import PilotSettings

OWNER = "g1-synthetic-owner"
TERMINAL = {"completed", "denied", "cancelled", "failed", "uncertain", "stale", "expired", "interrupted"}
SURFACES = {"textarea", "contenteditable", "iframe", "delayed"}


def _value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OfflinePilot:
    def __init__(self, settings: PilotSettings, ledger: Ledger, browser: FixtureBrowser, *, event_sink=None):
        if event_sink is not None and not callable(event_sink):
            raise TypeError("event_sink must be callable")
        self._event_sink = event_sink
        self.settings = settings
        self.ledger = ledger
        self.browser = browser
        self._gate = threading.RLock()
        self._write_gate = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="g1-controller")
        self._active: str | None = None
        self._closed = False
        self._sessions: dict[str, str] = {}
        self._metadata: dict[str, dict] = {}
        self._previews: OrderedDict[str, bytes] = OrderedDict()
        self.recovered = ledger.recover()
        ledger.seed_facts(OWNER, SEED_FACTS)
        self._emit_status("pilot_initialized")

    def _emit_status(self, event: str, run_id: str | None = None):
        """Best-effort observer after durable state changes; never an authority path."""
        if self._event_sink is None:
            return
        try:
            message = {
                "event": event,
                "gate": "G1",
                "live_enabled": False,
                "timestamp": round(time.time(), 3),
            }
            if run_id is not None:
                row = self.ledger.get_run(OWNER, run_id)
                message.update(
                    {
                        "run_id": row["id"],
                        "state": row["status"],
                        "action_id": row["action_id"],
                        "cancel_requested": row["cancel_requested"],
                    }
                )
            self._event_sink(message)
        except Exception:
            # Logging failure cannot approve, repeat, roll back or fail a task.
            pass

    def describe(self) -> dict:
        self._expire_active()
        return {
            **integration_status(),
            "memory_backend": self.ledger.search_backend,
            "recovered_unfinished_runs": self.recovered,
            "browser_kind": "real sandboxed Chromium, owned fixture only",
            "active_run_id": self._active,
            "fault_injection": self.settings.allow_fault_injection,
        }

    def _check_session(self, session: str):
        if not isinstance(session, str) or not 8 <= len(session) <= 128:
            raise ValueError("Invalid local session")

    def get_run(self, session: str, run_id: str) -> dict:
        self._check_session(session)
        row = self.ledger.get_run(OWNER, run_id)
        bound_session = row["proposal"]["binding"].get("session_id") if row["proposal"] else None
        with self._gate:
            known = self._sessions.get(run_id)
            meta = dict(self._metadata.get(run_id, {}))
        if (bound_session or known) != session:
            raise NotFound("Run not found for this local session")
        return {
            **row,
            "simulation": meta,
            "integration_status": "G1 offline; live integrations UNVERIFIED",
            "provider_cost_usd": 0,
            "preview_available": run_id in self._previews,
        }

    def _new_run(self, session: str, request_id: str, payload: dict) -> tuple[dict, bool]:
        self._check_session(session)
        payload = {**payload, "session_id": session, "mode": "offline"}
        with self._gate:
            if self._closed:
                raise Conflict("Pilot is closed")
            self._expire_active()
            # Identical retries must return the original run even when another run is active.
            # A rejected new run is terminalized, not left as orphaned preparing work.
            row, created = self.ledger.create_run(OWNER, request_id, payload)
            self._sessions[row["id"]] = session
            if not created:
                return self.get_run(session, row["id"]), False
            if self._active:
                prior = self.ledger.get_run(OWNER, self._active)
                if prior["status"] not in TERMINAL:
                    self.ledger.fail(
                        OWNER, row["id"], "Another task is active; use a new request ID after it ends"
                    )
                    raise Conflict("Another task is active; cancel or finish it first")
            self._active = row["id"]
            return row, True

    def start_fixture(self, session: str, request_id: str, fixture_id: str, surface="textarea") -> dict:
        fixture = scripted_transcript(fixture_id)
        if surface not in SURFACES:
            raise ValueError("Unknown fixture surface")
        row, created = self._new_run(
            session, request_id, {"kind": "draft", "fixture_id": fixture_id, "surface": surface}
        )
        if created:
            with self._gate:
                self._metadata[row["id"]] = {
                    "transcript": fixture.transcript,
                    "transcription": "scripted_fixture_not_STT",
                    "planner": "deterministic_not_LLM",
                    "speech": "synthetic_tone_not_TTS",
                    "fixture_id": fixture_id,
                }
            self._emit_status("run_requested", row["id"])
            self._pool.submit(self._prepare_browser, session, row["id"], fixture_id, surface)
        return self.get_run(session, row["id"])

    def _fact_binding(self, key: str) -> dict:
        fact = self.ledger.get_fact_state(OWNER, key)
        if fact is None:
            return {"key": key, "revision": None, "value_sha256": None}
        return {
            "key": key,
            "revision": fact["revision"],
            "value_sha256": None if fact["deleted"] else _value_hash(fact["value"]),
        }

    def _binding(self, session: str, run_id: str, kind: str, args: dict) -> dict:
        result = {
            "session_id": session,
            "run_id": run_id,
            "owner": OWNER,
            "mode": "offline",
            "fact": self._fact_binding(args["fact_key"]),
        }
        if kind == "fill_draft":
            result["page"] = self.browser.snapshot(run_id)
        return result

    def _prepare_browser(self, session: str, run_id: str, fixture_id: str, surface: str):
        try:
            if self.ledger.get_run(OWNER, run_id)["status"] != "preparing":
                return
            fixture = scripted_transcript(fixture_id)
            start = time.perf_counter()
            fact = self.ledger.get_fact(OWNER, fixture.fact_key)
            lookup_ms = (time.perf_counter() - start) * 1000
            if fact is None:
                raise Conflict("The required synthetic fact is absent or deleted; refusing to guess")
            text = draft_from_fixture(fixture, fact["value"])
            page = self.browser.open(run_id, surface)
            binding = {
                "session_id": session,
                "run_id": run_id,
                "owner": OWNER,
                "mode": "offline",
                "fact": {
                    "key": fact["key"],
                    "revision": fact["revision"],
                    "value_sha256": _value_hash(fact["value"]),
                },
                "page": page,
            }
            args = {"fact_key": fixture.fact_key, "fixture_id": fixture_id, "surface": surface, "text": text}
            with self._gate:
                self._metadata[run_id]["sqlite_lookup_ms"] = lookup_ms
                self._metadata[run_id]["fact_revision"] = fact["revision"]
            self._capture(run_id)
            # propose() itself refuses cancellation and other stale run state atomically.
            self.ledger.propose(
                run_id, "fill_draft", args, binding, ttl_seconds=self.settings.approval_ttl_seconds
            )
            self._emit_status("proposal_ready", run_id)
        except Exception as exc:
            self.ledger.fail(OWNER, run_id, self._reason(exc), uncertain=False)
            self._emit_status("preparation_failed", run_id)
            self._close_browser_run(run_id)
        finally:
            self._release_terminal(run_id)

    def propose_memory(
        self, session: str, request_id: str, key: str, value: str | None, *, delete=False
    ) -> dict:
        if not isinstance(key, str) or not key.startswith("synthetic.") or len(key) > 120:
            raise ValueError("G1 accepts synthetic.* keys only")
        if not delete and (not isinstance(value, str) or len(value) > 4000):
            raise ValueError("Synthetic value must be text of at most 4000 characters")
        row, created = self._new_run(
            session,
            request_id,
            {"kind": "delete_fact" if delete else "set_fact", "fact_key": key, "value": value},
        )
        if created:
            kind = "delete_fact" if delete else "set_fact"
            args = {"fact_key": key, "value": value}
            try:
                binding = self._binding(session, row["id"], kind, args)
                if delete and binding["fact"]["revision"] is None:
                    raise NotFound("Synthetic fact is absent or already deleted")
                self.ledger.propose(
                    row["id"], kind, args, binding, ttl_seconds=self.settings.approval_ttl_seconds
                )
                self._emit_status("memory_proposal_ready", row["id"])
            except Exception as exc:
                self.ledger.fail(OWNER, row["id"], self._reason(exc))
                self._release_terminal(row["id"])
                raise
        return self.get_run(session, row["id"])

    def decide(self, session: str, action_id: str, approved: bool, digest: str) -> dict:
        action = self.ledger.get_action(OWNER, action_id)
        self.get_run(session, action["run_id"])
        try:
            decided = self.ledger.decide(OWNER, action_id, approved, digest=digest)
        except Conflict:
            state = self.ledger.get_run(OWNER, action["run_id"])
            if state["status"] in TERMINAL:
                self._pool.submit(self._close_browser_run, action["run_id"])
                self._release_terminal(action["run_id"])
            raise
        self._emit_status("decision_recorded", action["run_id"])
        if approved:
            self._pool.submit(self._execute, session, action_id)
        else:
            self._pool.submit(self._close_browser_run, decided["run_id"])
            self._release_terminal(decided["run_id"])
        return self.get_run(session, decided["run_id"])

    def _execute(self, session: str, action_id: str):
        with self._write_gate:
            action = self.ledger.get_action(OWNER, action_id)
            run_id, kind, args = action["run_id"], action["kind"], action["args"]
            begun = False
            try:
                current = self._binding(session, run_id, kind, args)
                started = self.ledger.begin(OWNER, action_id, current_binding=current)
                begun = True
                self._emit_status("execution_started", run_id)
                if kind == "fill_draft":
                    # The browser worker rechecks the exact approved snapshot on its
                    # own thread before input. The owned fixture also checks its
                    # target atomically at save; this is not an arbitrary-site transaction guarantee.
                    receipt = self.browser.execute(
                        run_id, args["text"], expected_snapshot=started["binding"]["page"]
                    )
                    self._capture(run_id)
                elif kind == "set_fact":
                    expected = current["fact"]["revision"]
                    receipt = {
                        "fact": self.ledger.set_fact(
                            OWNER,
                            args["fact_key"],
                            args["value"],
                            expected_revision=expected if expected is not None else 0,
                        ),
                        "verified": True,
                        "kind": kind,
                    }
                elif kind == "delete_fact":
                    receipt = {
                        "fact": self.ledger.delete_fact(
                            OWNER, args["fact_key"], expected_revision=current["fact"]["revision"]
                        ),
                        "verified": True,
                        "kind": kind,
                    }
                else:
                    raise ValueError("Only owned fixture drafts and synthetic fact mutations exist in G1")
                receipt["provider_cost_usd"] = 0
                receipt["live_integrations"] = "UNVERIFIED"
                self.ledger.complete(OWNER, action_id, receipt)
                self._emit_status("execution_finished", run_id)
            except Exception as exc:
                # An exception after begin may follow a write. Do not hide that
                # ambiguity or perform a retry, even for an owned fixture.
                self.ledger.fail(OWNER, run_id, self._reason(exc), uncertain=begun)
                self._emit_status("execution_failed", run_id)
            finally:
                self._close_browser_run(run_id)
                self._release_terminal(run_id)

    def cancel(self, session: str, run_id: str) -> dict:
        self.get_run(session, run_id)
        row = self.ledger.cancel(OWNER, run_id)
        self._emit_status("cancellation_recorded", run_id)
        if row["status"] != "executing":
            self._pool.submit(self._close_browser_run, run_id)
        self._release_terminal(run_id)
        return self.get_run(session, run_id)

    def fault(self, session: str, run_id: str, kind: str) -> dict:
        with self._write_gate:
            if not self.settings.allow_fault_injection:
                raise Conflict("Controlled fault injection is disabled")
            row = self.get_run(session, run_id)
            if row["status"] != "pending" or row["proposal"]["kind"] != "fill_draft":
                raise Conflict("Fault injection is only available on a pending owned-fixture proposal")
            if kind == "memory":
                key = row["proposal"]["args"]["fact_key"]
                fact = self.ledger.get_fact(OWNER, key)
                if fact is None:
                    raise Conflict("Synthetic fact no longer exists")
                self.ledger.set_fact(
                    OWNER,
                    key,
                    fact["value"] + " [controlled synthetic revision]",
                    expected_revision=fact["revision"],
                )
            else:
                self.browser.tamper(run_id, kind)
            self._capture(run_id)
            return self.get_run(session, run_id)

    def screenshot(self, session: str, run_id: str) -> bytes:
        self.get_run(session, run_id)
        with self._gate:
            image = self._previews.get(run_id)
        if image is None:
            raise NotFound("No fixture preview exists for this run")
        return image

    def events(self, session: str, run_id: str) -> list[dict]:
        self.get_run(session, run_id)
        return self.ledger.events(OWNER, run_id)

    def _capture(self, run_id):
        try:
            image = self.browser.screenshot(run_id)
        except Exception:
            return  # Optional evidence cannot turn a verified write into failure.
        if len(image) > 2_000_000:
            return
        with self._gate:
            self._previews[run_id] = image
            self._previews.move_to_end(run_id)
            while len(self._previews) > 5:
                self._previews.popitem(last=False)

    @staticmethod
    def _reason(exc):
        # Inputs are synthetic; still avoid embedding arbitrary tracebacks or secrets.
        if isinstance(exc, (ValueError, Conflict, NotFound, FixtureViolation)):
            return str(exc)[:500] or type(exc).__name__
        return f"{type(exc).__name__}: operation not confirmed; inspect local test diagnostics"

    def _expire_active(self):
        with self._gate:
            if self._active:
                run_id = self._active
                row = self.ledger.expire_run(OWNER, run_id)
                if row["status"] in TERMINAL:
                    self._pool.submit(self._close_browser_run, run_id)
                    self._active = None

    def abandon_session(self, session: str):
        """Session eviction/loss cannot strand a pending task or silently resume it."""
        with self._gate:
            if not self._active:
                return
            run_id = self._active
            row = self.ledger.get_run(OWNER, run_id)
            bound = (
                row["proposal"]["binding"].get("session_id")
                if row["proposal"]
                else self._sessions.get(run_id)
            )
            if bound != session:
                return
            row = self.ledger.cancel(OWNER, run_id)
            if row["status"] != "executing":
                self._pool.submit(self._close_browser_run, run_id)
            self._release_terminal(run_id)

    def _close_browser_run(self, run_id):
        with suppress(Exception):
            self.browser.close_run(run_id)

    def _release_terminal(self, run_id):
        with self._gate:
            if self._active == run_id and self.ledger.get_run(OWNER, run_id)["status"] in TERMINAL:
                self._active = None

    def close(self):
        with self._gate:
            if self._closed:
                return
            self._closed = True
            if self._active:
                self.ledger.cancel(OWNER, self._active)
        self._pool.shutdown(wait=True, cancel_futures=False)
        self.browser.close()
