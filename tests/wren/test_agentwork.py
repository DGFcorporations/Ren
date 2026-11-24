"""Tests for the agent work-through layer: ledger, approvals, policy, routing, verify."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from wren.agentwork import (
    AgentWorkLedger,
    AgentWorkspace,
    ApprovalBridge,
    LimitExceeded,
    Router,
)

DB = None


def _db():
    global DB
    if DB is None:
        tmp = tempfile.TemporaryDirectory()
        DB = (tmp, str(Path(tmp.name) / "agentwork.db"))
    return DB[1]


@pytest.fixture(autouse=True)
def _clean_db():
    yield
    # fresh db file per test run is enough because we use unique session ids
    pass


def test_ledger_reserve_progress_complete_and_idempotent():
    ledger = AgentWorkLedger(_db())
    sess = ledger.ensure_session("s1", "alice")
    assert sess["agent"] == "alice"

    action, is_new = ledger.reserve_action("s1", "alice", "ren_files", {"op": "write"}, idempotency_key="k1")
    assert is_new is True
    assert action["status"] == "running"
    ledger.update_progress(action["id"], 0.5)
    assert ledger.get_action(action["id"])["progress"] == 0.5

    receipt = {"result": {"ok": True}, "verified": {"verified": True}}
    done = ledger.complete(action["id"], receipt)
    assert done["status"] == "completed"
    assert done["receipt"]["result"]["ok"] is True

    # Idempotent replay returns the same terminal outcome
    action2, is_new2 = ledger.reserve_action(
        "s1", "alice", "ren_files", {"op": "write"}, idempotency_key="k1"
    )
    assert is_new2 is False
    assert action2["status"] == "completed"


def test_policy_limit_exceeded():
    ledger = AgentWorkLedger(_db())
    ledger.set_policy("bob", quota_per_min=2)
    ledger.check_limit("bob")
    ledger.check_limit("bob")
    with pytest.raises(LimitExceeded):
        ledger.check_limit("bob")


def test_policy_write_disallowed():
    ledger = AgentWorkLedger(_db())
    ledger.set_policy("readonly", write_allowed=0)
    with pytest.raises(LimitExceeded):
        ledger.check_limit("readonly", write=True)


def test_approval_bridge_approve_then_execute():
    ledger = AgentWorkLedger(_db())
    bridge = ApprovalBridge(ledger)
    action, _ = ledger.reserve_action("s2", "carol", "ren_files", {"op": "write"})
    ledger._transition(action["id"], "awaiting_approval")

    pending = bridge.open(action["id"], "ren_files", {"path": "x.txt"})
    assert pending["status"] == "pending"

    resolved = bridge.resolve(action["id"], True)
    assert resolved["status"] == "approved"
    assert ledger.get_action(action["id"])["status"] == "approved"

    # denied path
    action2, _ = ledger.reserve_action("s2", "carol", "ren_browser", {"op": "click"})
    bridge.open(action2["id"], "ren_browser", {})
    bridge.resolve(action2["id"], False)
    assert ledger.get_action(action2["id"])["status"] == "denied"


def test_agentworkspace_execute_envelope_and_verify():
    class FakeWorkspace:
        def __init__(self):
            self.written = []

        def write(self, rel, content, confirm=False):
            self.written.append((rel, content))
            return {"type": "write_preview", "committed": confirm}

    class FakeController:
        def __init__(self):
            self.workspace = FakeWorkspace()

    ctrl = FakeController()
    ws = AgentWorkspace(ctrl, ledger=AgentWorkLedger(_db()))
    sess = ws.begin("s3", "dave")

    def _write():
        return ctrl.workspace.write("a.txt", "hello", confirm=True)

    out = ws.execute(
        sess["session_id"], "dave", "ren_files", {"op": "write"},
        _write, require_approval=True, verify_fn=lambda: {"verified": True}, write=True,
    )
    assert out["type"] == "pending_approval"
    assert out["requires_confirm"] is True
    action_id = out["action_id"]

    ws.bridge.resolve(action_id, True)
    final = ws.execute_after_approval(
        action_id, _write, verify_fn=lambda: {"verified": True}
    )
    assert final["status"] == "completed"
    assert final["schema"] == "agentwork.action.v1"
    assert final["verified"]["verified"] is True
    assert ctrl.workspace.written == [("a.txt", "hello")]


def test_live_diff_tracks_changes():
    ledger = AgentWorkLedger(_db())
    ws = AgentWorkspace(object(), ledger=ledger)
    sess = ws.begin("s4", "erin")
    diff = ws.live_diff(sess["session_id"])

    snap1 = {"files": [{"path": "a.py", "size_bytes": 3, "truncated": False}],
             "desktop": {"active_window": {"title": "Zed"}, "open_windows": []},
             "has_screenshot": False}
    r1 = diff.update(snap1)
    assert r1["changed"] is True

    snap2 = {"files": [{"path": "a.py", "size_bytes": 99, "truncated": False}],
             "desktop": {"active_window": {"title": "Chrome"}, "open_windows": []},
             "has_screenshot": False}
    r2 = diff.update(snap2)
    assert r2["changed"] is True
    d = diff.diff(snap1, snap2)
    assert d["active_window_changed"] is True
    assert "a.py" in d["files_modified"]


def test_router_ranks_by_capability():
    class FakeMemory:
        def agents(self, fresh_s=None):
            return [
                {"name": "weak", "capabilities": ["shell"], "age_s": 5},
                {"name": "strong", "capabilities": ["shell", "browser"], "age_s": 2},
            ]

    class FakeController:
        memory = FakeMemory()

    candidates = Router(FakeController()).route("open a page", ["browser"])
    assert candidates and candidates[0]["name"] == "strong"


def test_mcp_work_tools_via_local_dispatch():
    from wren.mcp import server as mcp_server

    class FakeController:
        def __init__(self):
            self.agentwork = AgentWorkspace(self, ledger=AgentWorkLedger(_db()))
            self.memory = type("M", (), {"agents": staticmethod(lambda fresh_s=None: [])})()
            self.browser = type("B", (), {"_page": None})()

    controller = FakeController()
    token = mcp_server.local_controller.set(controller)
    try:
        begin = mcp_server.dispatch("ren_work_begin", {"agent": "agent"})
        assert begin["type"] == "session_ready"
        sid = begin["session_id"]

        actions = mcp_server.dispatch("ren_work_actions", {"session_id": sid})
        assert actions["type"] == "agentwork_actions"

        poll = mcp_server.dispatch("ren_work_poll", {"session_id": sid, "after_id": 0})
        assert poll["type"] == "agentwork_events"

        approvals = mcp_server.dispatch("ren_work_approvals", {})
        assert approvals["type"] == "pending_approvals"

        policy = mcp_server.dispatch("ren_work_policy", {"agent": "agent", "get": True})
        assert policy["agent"] == "agent"
    finally:
        mcp_server.local_controller.reset(token)
