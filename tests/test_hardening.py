"""Regression cases found during independent cross-component review."""

import re
import time

import httpx
import pytest

from workbench_pilot.controller import OWNER

from .test_app import decision, live_app, start, wait


@pytest.mark.browser
@pytest.mark.parametrize("rejection", ["stale", "expired"])
def test_rejected_approvals_do_not_exhaust_browser_contexts(tmp_path, rejection):
    with live_app(tmp_path / "data", ttl=0.05 if rejection == "expired" else 300) as (client, app, _):
        # The browser cap is five. Every rejected run must release its context.
        for index in range(7):
            row = wait(client, start(client, request_id=f"rejected-{index}")["id"])
            if rejection == "expired":
                time.sleep(0.06)
                response = decision(client, row)
            else:
                response = decision(client, row, digest="0" * 64)
            assert response.status_code == 409
            assert client.get("/api/pilot/runs/" + row["id"]).json()["status"] == rejection
        assert app.state.pilot.describe()["active_run_id"] is None


def test_session_eviction_cancels_stranded_task(tmp_path):
    with live_app(tmp_path / "data") as (client, app, origin):
        old = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "old-session", "key": "synthetic.old", "value": "never approved"},
        ).json()
        for _ in range(32):
            with httpx.Client(base_url=origin, trust_env=False) as other:
                assert other.get("/").status_code == 200
        assert client.get("/api/pilot/status").status_code == 401
        assert app.state.pilot.ledger.get_run(OWNER, old["id"])["status"] == "cancelled"
        html = client.get("/").text
        client.headers["X-Pilot-CSRF"] = re.search(r'name="pilot-csrf" content="([0-9a-f]+)"', html)[1]
        fresh = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "fresh-session", "key": "synthetic.fresh", "value": "new proposal"},
        )
        assert fresh.status_code == 200
        assert fresh.json()["status"] == "pending"
        assert app.state.pilot.ledger.get_fact(OWNER, "synthetic.old") is None


def test_expired_abandoned_task_does_not_block_new_session(tmp_path):
    with live_app(tmp_path / "data", ttl=0.1) as (client, app, origin):
        old = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "abandoned", "key": "synthetic.abandoned", "value": "never"},
        ).json()
        time.sleep(0.12)
        with httpx.Client(base_url=origin, trust_env=False) as fresh:
            html = fresh.get("/").text
            fresh.headers["X-Pilot-CSRF"] = re.search(r'name="pilot-csrf" content="([0-9a-f]+)"', html)[1]
            result = fresh.post(
                "/api/pilot/memory/proposals",
                json={"request_id": "after-expiry", "key": "synthetic.new", "value": "pending"},
            )
            assert result.status_code == 200, result.text
        assert app.state.pilot.ledger.get_run(OWNER, old["id"])["status"] == "expired"


def test_explicit_approved_recreation_uses_tombstone_revision(tmp_path):
    with live_app(tmp_path / "data") as (client, app, _):
        key = "synthetic.helix.kickoff"
        deleted = client.post(
            "/api/pilot/memory/proposals", json={"request_id": "delete", "key": key, "delete": True}
        ).json()
        assert decision(client, deleted).status_code == 200
        wait(client, deleted["id"], "completed")
        assert app.state.pilot.ledger.get_fact(OWNER, key) is None
        tombstone = app.state.pilot.ledger.get_fact_state(OWNER, key)
        assert tombstone["deleted"] and tombstone["value"] is None
        app.state.pilot.ledger.seed_facts(OWNER, {key: "automatic resurrection forbidden"})
        assert app.state.pilot.ledger.get_fact(OWNER, key) is None
        new = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "recreate", "key": key, "value": "Explicitly approved synthetic value"},
        ).json()
        assert new["proposal"]["binding"]["fact"]["revision"] == tombstone["revision"]
        assert decision(client, new).status_code == 200
        wait(client, new["id"], "completed")
        fact = app.state.pilot.ledger.get_fact(OWNER, key)
        assert fact["value"] == "Explicitly approved synthetic value"
        assert fact["revision"] == tombstone["revision"] + 1
