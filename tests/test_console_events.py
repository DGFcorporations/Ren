"""Optional console observation must neither leak payloads nor alter execution semantics."""

import json

import pytest

from .test_app import decision, live_app, wait

pytestmark = pytest.mark.monitor


def test_console_events_are_metadata_only(tmp_path):
    events = []
    with live_app(tmp_path / "data", event_sink=events.append) as (client, app, _):
        row = client.post(
            "/api/pilot/memory/proposals",
            json={
                "request_id": "console-events",
                "key": "synthetic.console",
                "value": "DO-NOT-LOG-SYNTHETIC-PAYLOAD",
            },
        ).json()
        assert decision(client, row).status_code == 200
        wait(client, row["id"], "completed")
        names = [event["event"] for event in events]
        assert "pilot_initialized" in names
        assert "memory_proposal_ready" in names
        assert "execution_started" in names
        assert "execution_finished" in names
        serialized = json.dumps(events)
        assert "DO-NOT-LOG-SYNTHETIC-PAYLOAD" not in serialized
        assert row["proposal"]["digest"] not in serialized
        assert all(
            set(event)
            <= {
                "event",
                "gate",
                "live_enabled",
                "timestamp",
                "run_id",
                "state",
                "action_id",
                "cancel_requested",
            }
            for event in events
        )


def test_logging_failure_cannot_fail_or_repeat_an_action(tmp_path):
    def failing_sink(event):
        raise OSError("Synthetic observer failure")

    with live_app(tmp_path / "data", event_sink=failing_sink) as (client, app, _):
        row = client.post(
            "/api/pilot/memory/proposals",
            json={"request_id": "broken-observer", "key": "synthetic.console", "value": "v"},
        ).json()
        assert decision(client, row).status_code == 200
        completed = wait(client, row["id"], "completed")
        assert completed["receipt"]["fact"]["revision"] == 1
        assert decision(client, row).status_code == 409
