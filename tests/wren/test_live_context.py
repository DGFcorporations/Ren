"""Tests for live multimodal conversation, desktop/file context, and MCP access."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from wren.core import live_context
from wren.mcp import server as mcp_server


def test_live_snapshotter_reads_workspace_files_and_interfaces():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes.md").write_text("# Ren Live Notes\nWorking on MCP.", encoding="utf-8")
        (root / "app.py").write_text("print('hello ren')\n", encoding="utf-8")

        snap = live_context.LiveContextSnapshotter(workspace_root=root)
        with (
            patch.object(
                snap,
                "read_desktop_interfaces",
                return_value={
                    "active_window": {"window_id": "101", "title": "Zed - Ren", "wm_class": "zed"},
                    "open_windows": [{"window_id": "101", "title": "Zed - Ren", "is_chat_app": True}],
                    "chat_windows": [{"window_id": "101", "title": "Zed - Ren", "is_chat_app": True}],
                },
            ),
            patch.object(snap, "capture_screenshot_b64", return_value="iVBORw0KGgo="),
        ):
            result = snap.capture(include_screenshot=True)
            assert result["type"] == "live_context"
            assert result["has_screenshot"] is True
            assert len(result["files"]) == 2
            assert result["desktop"]["active_window"]["title"] == "Zed - Ren"


def test_live_conversation_engine_shares_history_across_turns():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "main.py").write_text("def run():\n    return 42\n", encoding="utf-8")
        snap = live_context.LiveContextSnapshotter(workspace_root=root)
        engine = live_context.LiveConversationEngine(snapshotter=snap)

        with (
            patch.object(
                snap,
                "read_desktop_interfaces",
                return_value={"active_window": {"title": "Slack - #dev"}, "open_windows": []},
            ),
            patch.object(snap, "capture_screenshot_b64", return_value=""),
        ):
            turn1 = engine.converse("What does main.py return?", role="user")
            assert "main.py" in turn1["files_seen"]
            assert turn1["active_window"] == "Slack - #dev"
            assert turn1["history_length"] == 2

            turn2 = engine.converse("Summarize that for the Slack window.", role="agent")
            assert turn2["history_length"] == 4
            assert len(engine.get_history()) == 4


def test_mcp_live_context_returns_native_image_block_and_converse_works():
    fake_snapshot = {
        "type": "live_context",
        "timestamp": 1700000000.0,
        "workspace": "/tmp/ren",
        "files": [{"path": "README.md", "size_bytes": 12, "truncated": False, "content": "# Ren"}],
        "desktop": {"active_window": {"title": "Mattermost"}, "open_windows": []},
        "has_screenshot": True,
        "screenshot_b64": "ZmFrZV9wbmc=",
    }

    engine = mcp_server._get_local_live_engine()
    with patch.object(engine.snapshotter, "capture", return_value=fake_snapshot):
        resp = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "ren_live_context",
                    "arguments": {"include_screenshot": True},
                },
            }
        )
        assert resp is not None
        content = resp["result"]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert "screenshot_b64" not in content[0]["text"]
        assert content[1] == {"type": "image", "data": "ZmFrZV9wbmc=", "mimeType": "image/png"}

    conv_resp = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "ren_live_converse",
                "arguments": {"text": "Check Mattermost and README.md"},
            },
        }
    )
    assert conv_resp is not None
    conv_content = conv_resp["result"]["content"][0]["text"]
    payload = json.loads(conv_content)
    assert payload["type"] == "live_reply"
    assert len(payload["files_seen"]) > 0


def test_live_snapshotter_get_chat_non_none_and_skips_escaping_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "ok.txt").write_text("safe", encoding="utf-8")
        snap = live_context.LiveContextSnapshotter(workspace_root=root)
        assert snap._get_chat() is not None
        files = snap.read_current_files(["../outside.txt", "ok.txt"])
        assert [f["path"] for f in files] == ["ok.txt"]
