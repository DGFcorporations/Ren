"""Stdio MCP bridge so other agents can call the running Ren workstation.

Ren itself must already be running (`wren`). When this module runs inside the
Ren server process (the HTTP MCP transport), tool calls run directly against
the live controller — no HTTP self-call. As a standalone stdio process it
forwards tool calls to the loopback API. It does not open its own browser or
microphone.
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

PROTOCOL = "2024-11-05"
REN_URL = os.environ.get("REN_URL", "http://127.0.0.1:8765").rstrip("/")

# Set per-request by the HTTP MCP transport so tool calls reach the same server
# the client addressed (any port / host / future tunnel scheme). stdio leaves it unset.
request_base_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wren_ren_request_base_url", default=None
)

# Set per-request by the in-process HTTP MCP transport to the live controller.
# When set, dispatch() runs tools directly against the controller and never
# makes an HTTP call to the REST API — so a remote MCP client behind a proxy
# (Cloudflare) cannot break the tools by blocking the self-call. stdio leaves
# it unset and keeps the loopback-HTTP path. The controller is typed Any here
# on purpose: the stdio bridge must not import the controller graph.
local_controller: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "wren_local_controller", default=None
)

# Resolved bearer-token identity ({'name','scopes'}) set per request by the
# HTTP transport. dispatch() enforces scope on every call: 'read' agents get
# read-only tools only. stdio leaves it None -> full local access.
current_agent: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "wren_current_agent", default=None
)

TOOLS = [
    {
        "name": "ren_status",
        "description": "Read Ren's mode, dictation state, current paste preview, and last reply.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ren_converse",
        "description": "Send a conversational turn to Ren and receive the spoken reply. Does not paste.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "ren_dictate",
        "description": "Stage dictated text as a clipboard preview. Does not paste until ren_paste_commit.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "ren_screen_read",
        "description": "Read the phone screen bridge and the workstation's active window.",
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string", "enum": ["phone", "desktop", "both"]}},
        },
    },
    {
        "name": "ren_paste_preview",
        "description": "Return the text currently waiting for a paste confirmation.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ren_paste_commit",
        "description": (
            "Paste the current preview into the focused workstation window. Requires confirm true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"confirm": {"type": "boolean"}},
            "required": ["confirm"],
        },
    },
    {
        "name": "ren_browser",
        "description": (
            "Control Ren's owned Chromium. snapshot and navigate run immediately. "
            "click, type, move, and scroll require confirm true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["snapshot", "navigate", "click", "type", "move", "scroll"]},
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "delta_y": {"type": "number"},
                "confirm": {"type": "boolean"},
                "approval_id": {"type": "string"},
            },
            "required": ["op"],
        },
    },
    {
        "name": "ren_files",
        "description": "List, read, or write files inside WREN_WORKSPACE. Writes require confirm true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["list", "read", "write"]},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "confirm": {"type": "boolean"},
                "approval_id": {"type": "string"},
            },
            "required": ["op"],
        },
    },
    {
        "name": "ren_set_mode",
        "description": "Set Ren's mode to elevate, agent, or brainstorm.",
        "inputSchema": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["elevate", "agent", "brainstorm"]}},
            "required": ["mode"],
        },
    },
    {
        "name": "ren_agent_hello",
        "description": "Announce this agent to the registry: name + capabilities. "
        "Call once after initialize so Ren knows who is connected and what they can do.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "capabilities": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name"],
        },
    },
    {
        "name": "ren_memory_remember",
        "description": "Store a durable fact (key/value/tags). "
        "tags 'correction' feeds the STT spelling glossary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "tags": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "ren_memory_recall",
        "description": "Full-text recall over stored facts. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "ren_memory_forget",
        "description": "Delete a stored fact by key.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "ren_handoff_create",
        "description": "Queue a task for an agent to claim later. requires lists needed capabilities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "requires": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task"],
        },
    },
    {
        "name": "ren_handoff_claim",
        "description": "Claim the oldest open handoff your capabilities satisfy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "capabilities": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "ren_handoff_complete",
        "description": "Report a claimed handoff done (or failed) with its result text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "result": {"type": "string"},
                "ok": {"type": "boolean"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "ren_handoff_list",
        "description": "List handoffs, optionally filtered by status (open/claimed/done/failed). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
        },
    },
    {
        "name": "ren_speak",
        "description": "Speak text aloud on the workstation through Ren's TTS chain — "
        "remote agents borrow his voice.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "ren_live_context",
        "description": (
            "Capture a live snapshot of current workspace files, open desktop app interfaces, "
            "the active window, and an optional PNG screenshot (returned as an MCP image block)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_screenshot": {
                    "type": "boolean",
                    "description": "If true, capture and attach a PNG screenshot of the desktop.",
                },
                "include_files": {"type": "boolean"},
                "include_windows": {"type": "boolean"},
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional specific workspace file paths to inspect.",
                },
            },
        },
    },
    {
        "name": "ren_live_converse",
        "description": (
            "Run a live conversation turn with Ren grounded in current workspace files, "
            "desktop app interfaces, and optional screen capture; shares history with the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message or question for Ren."},
                "role": {"type": "string", "enum": ["user", "agent"]},
                "include_screenshot": {"type": "boolean"},
                "include_files": {"type": "boolean"},
                "include_windows": {"type": "boolean"},
                "file_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
    },
    {
        "name": "ren_live_history",
        "description": "Read or clear the shared live conversation history between user, Ren, and MCP.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["get", "clear"]},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "ren_work_begin",
        "description": (
            "Open (or resume) a durable agent work session. Returns a session_id "
            "you carry on later tool calls for continuity and ledger receipts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "context": {"type": "object"},
            },
        },
    },
    {
        "name": "ren_work_actions",
        "description": "List durable actions for a session, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "queued", "running", "awaiting_approval", "approved", "executing",
                        "completed", "cancelled", "failed", "denied", "expired",
                    ],
                },
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "ren_work_status",
        "description": "Inspect one action: status, progress, receipt, and verified outcome.",
        "inputSchema": {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
        },
    },
    {
        "name": "ren_work_cancel",
        "description": "Request cancellation of a running action.",
        "inputSchema": {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
        },
    },
    {
        "name": "ren_work_poll",
        "description": "Pull events (progress, actions, approvals) since an event id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "after_id": {"type": "integer"},
                "topics": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "ren_work_approvals",
        "description": "List pending async approvals awaiting resolve (user or agent).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ren_work_resolve",
        "description": "Approve or deny a pending action approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
                "approved": {"type": "boolean"},
            },
            "required": ["action_id", "approved"],
        },
    },
    {
        "name": "ren_work_policy",
        "description": "Get or set per-agent policy (rate quotas, write allow).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "get": {"type": "boolean"},
                "quota_per_min": {"type": "number"},
                "quota_per_day": {"type": "number"},
                "write_allowed": {"type": "boolean"},
            },
        },
    },
    {
        "name": "ren_work_route",
        "description": "Route a task to the best online agent by required capabilities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "requires": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task"],
        },
    },
    {
        "name": "ren_visual_query",
        "description": "Semantic grounding: map a text query to on-screen/browser elements by label.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]


class MCPError(RuntimeError):
    pass


_LOCAL_CHAT_CONTROLLER = None
_LOCAL_LIVE_ENGINE = None


def _get_local_chat_controller():
    global _LOCAL_CHAT_CONTROLLER
    if _LOCAL_CHAT_CONTROLLER is None:
        try:
            from wren.targets.chat_windows import DesktopChatController
        except ImportError:
            from chat_windows import DesktopChatController  # type: ignore[no-redef]
        _LOCAL_CHAT_CONTROLLER = DesktopChatController()
    return _LOCAL_CHAT_CONTROLLER


def _get_local_live_engine():
    global _LOCAL_LIVE_ENGINE
    if _LOCAL_LIVE_ENGINE is None:
        try:
            from wren.core.live_context import LiveConversationEngine
        except ImportError:
            from live_context import LiveConversationEngine  # type: ignore[no-redef]
        _LOCAL_LIVE_ENGINE = LiveConversationEngine()
    return _LOCAL_LIVE_ENGINE


def _request(method: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    base = (request_base_url.get() or REN_URL).rstrip("/")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise MCPError(f"{method} {path} failed ({exc.code}): {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise MCPError(
            f"Ren is not reachable at {REN_URL}. Start it with ./run_ren.sh. {exc.reason}"
        ) from exc


def _handle_live_context_local(controller: Any, args: dict) -> dict:
    """Handle ren_live_context against the live controller."""
    try:
        from wren.core.live_context import LiveContextSnapshotter
    except ImportError:
        from live_context import LiveContextSnapshotter  # type: ignore[no-redef]

    snapshotter = getattr(controller, "_live_snapshotter", None)
    if snapshotter is None:
        snapshotter = LiveContextSnapshotter()
        controller._live_snapshotter = snapshotter

    include_screenshot = bool(args.get("include_screenshot", False))
    include_files = bool(args.get("include_files", True))
    include_windows = bool(args.get("include_windows", True))
    file_paths = args.get("file_paths") or []

    return snapshotter.capture(
        include_files=include_files,
        include_windows=include_windows,
        include_screenshot=include_screenshot,
        file_paths=file_paths or None,
    )


def _handle_live_converse_local(controller: Any, args: dict) -> dict:
    """Handle ren_live_converse against the live controller."""
    try:
        from wren.core.live_context import LiveConversationEngine
    except ImportError:
        from live_context import LiveConversationEngine  # type: ignore[no-redef]

    engine = getattr(controller, "_live_conversation_engine", None)
    if engine is None:
        snapshotter = getattr(controller, "_live_snapshotter", None)
        if snapshotter is None:
            from wren.core.live_context import LiveContextSnapshotter
            snapshotter = LiveContextSnapshotter()
            controller._live_snapshotter = snapshotter
        engine = LiveConversationEngine(controller=controller, snapshotter=snapshotter)
        controller._live_conversation_engine = engine

    return engine.converse(
        args.get("text", ""),
        role=args.get("role", "user"),
        include_screenshot=bool(args.get("include_screenshot", False)),
        include_files=bool(args.get("include_files", True)),
        include_windows=bool(args.get("include_windows", True)),
        file_paths=args.get("file_paths") or None,
    )


def _handle_live_history_local(controller: Any, args: dict) -> dict:
    """Handle ren_live_history against the live controller."""
    try:
        from wren.core.live_context import LiveConversationEngine
    except ImportError:
        from live_context import LiveConversationEngine  # type: ignore[no-redef]

    engine = getattr(controller, "_live_conversation_engine", None)
    if engine is None:
        snapshotter = getattr(controller, "_live_snapshotter", None)
        if snapshotter is None:
            from wren.core.live_context import LiveContextSnapshotter
            snapshotter = LiveContextSnapshotter()
            controller._live_snapshotter = snapshotter
        engine = LiveConversationEngine(controller=controller, snapshotter=snapshotter)
        controller._live_conversation_engine = engine

    op = args.get("op", "get")
    limit = int(args.get("limit", 30))
    if op == "clear":
        return engine.clear_history()
    return {"turns": engine.get_history(limit=limit)}


def _run_execute(
    controller: Any,
    name: str,
    args: dict,
    fn,
    *,
    require_approval: bool = False,
    verify_fn=None,
    write: bool = False,
) -> dict:
    """Route a mutating tool through the AgentWorkspace ledger + approval bridge."""
    ws, session_id, agent = _agentwork_session(controller, args)
    return ws.execute(
        session_id,
        agent,
        name,
        args,
        fn,
        require_approval=require_approval,
        approval_summary={"tool": name, "args": {k: v for k, v in args.items() if k != "work"}},
        verify_fn=verify_fn,
        write=write,
    )


def _agentwork_session(controller: Any, args: dict, agent: str | None = None) -> tuple:
    """Resolve session_id + agent for work tiles."""
    agent = agent or (current_agent.get() or {}).get("name") or "agent"
    session_id = (args.get("session_id") or "").strip() or None
    ws = controller.agentwork
    sess = ws.begin(session_id or uuid.uuid4().hex, agent)
    return ws, sess["session_id"], agent


def _handle_work_begin(controller: Any, args: dict) -> dict:
    _, session_id, agent = _agentwork_session(controller, args)
    sess = controller.agentwork.ledger.get_session(session_id)
    return {"type": "session_ready", "session_id": session_id, "agent": agent, "created": sess["created"]}


def _handle_work_actions(controller: Any, args: dict) -> dict:
    ws, session_id, _ = _agentwork_session(controller, args)
    actions = ws.ledger.list_actions(
        session_id=session_id, status=args.get("status"), limit=args.get("limit", 50)
    )
    return {"type": "agentwork_actions", "session_id": session_id, "count": len(actions), "actions": actions}


def _handle_work_status(controller: Any, args: dict) -> dict:
    return controller.agentwork.ledger.get_action(args["action_id"])


def _handle_work_cancel(controller: Any, args: dict) -> dict:
    return controller.agentwork.ledger.request_cancel(args["action_id"])


def _handle_work_poll(controller: Any, args: dict) -> dict:
    ws, session_id, _ = _agentwork_session(controller, args)
    events = ws.ledger.poll_events(
        session_id=session_id, after_id=args.get("after_id", 0), topics=args.get("topics")
    )
    return {"type": "agentwork_events", "session_id": session_id, "events": events}


def _handle_work_approvals(controller: Any, args: dict) -> dict:
    pending = controller.agentwork.bridge.list_pending()
    return {"type": "pending_approvals", "count": len(pending), "approvals": pending}


def _handle_work_resolve(controller: Any, args: dict) -> dict:
    return controller.agentwork.bridge.resolve(args["action_id"], bool(args["approved"]))


def _handle_work_policy(controller: Any, args: dict) -> dict:
    agent = args.get("agent") or (current_agent.get() or {}).get("name") or "agent"
    if args.get("get"):
        return controller.agentwork.ledger.get_policy(agent)
    return controller.agentwork.ledger.set_policy(
        agent,
        quota_per_min=args.get("quota_per_min"),
        quota_per_day=args.get("quota_per_day"),
        write_allowed=args.get("write_allowed"),
    )


def _handle_work_route(controller: Any, args: dict) -> dict:
    candidates = controller.agentwork.router.route(args.get("task", ""), args.get("requires"))
    return {"type": "route_result", "task": args.get("task"), "candidates": candidates}


def _handle_visual_query(controller: Any, args: dict) -> dict:
    snap = getattr(controller, "live_snapshotter", None)
    live_text = ""
    if snap is not None:
        try:
            cap = snap.capture(include_files=True, include_windows=True, include_screenshot=False)
            live_text = "\n".join(f.get("content", "") for f in cap.get("files", [])[:4])
        except Exception:
            live_text = ""
    hits = controller.agentwork.grounding.visual_query(args.get("query", ""), live_text)
    return {"type": "visual_query_result", "query": args.get("query"), "hits": hits}


def _dispatch_local(controller: Any, name: str, args: dict) -> dict:
    """Run a tool directly against the live controller, without any HTTP call.

    This is the path the in-process HTTP MCP transport uses. Every branch
    mirrors the REST route handler in ``wren.ui.orb_preview`` for the same
    operation (same controller call, same result shape, same confirm gates),
    so the tool contract is identical whether the caller reaches Ren over
    stdio (loopback HTTP) or over MCP-HTTP (direct). Controller failures
    surface as MCPError, exactly like an HTTP 400 from the REST route.
    """
    try:
        if name == "ren_status":
            # mcp_http_mounted is True by construction: this path only runs
            # when the MCP HTTP transport is active.
            return controller.status_dict(True)
        if name == "ren_converse":
            # REST defaults: src="orb", speak=False — the MCP call sends text only.
            reply = controller.converse(args.get("text", ""), source="orb")
            return {"type": "assistant_message", "text": reply, "requires_confirm": True}
        if name == "ren_dictate":
            draft = controller.accept_dictation(args.get("text", ""), source="mcp")
            return {
                "type": "paste_preview",
                "requires_confirm": True,
                "raw": args.get("text", ""),
                "status": "draft_ready",
                "revision": draft.revision,
                "text": draft.text,
            }
        if name == "ren_screen_read":
            source = args.get("source") or "both"
            out: dict = {"source": source}
            if source in {"phone", "both"}:
                out["phone"] = controller.phone_screen
            if source in {"desktop", "both"}:
                ctx = controller.get_screen_context(capture_screenshot=False)
                out["desktop"] = {
                    "app_name": ctx.app_name,
                    "window_title": ctx.window_title,
                    "window_id": ctx.window_id,
                    "is_browser": ctx.is_browser,
                    "is_editor": ctx.is_editor,
                    "is_terminal": ctx.is_terminal,
                    "timestamp": ctx.timestamp,
                }
            return out
        if name == "ren_paste_preview":
            return controller.paste_preview()
        if name == "ren_paste_commit":
            if args.get("work"):
                def _paste():
                    receipt = controller.paste_current_draft()
                    return {"status": "pasted", "outcome": receipt.outcome, "evidence": receipt.evidence}

                def verify():
                    return controller.agentwork.verify.paste_receipt()

                if args.get("action_id"):
                    return controller.agentwork.execute_after_approval(args["action_id"], _paste, verify)
                if args.get("confirm") is not True:
                    return _run_execute(
                        controller, name, args, _paste,
                        require_approval=True, verify_fn=verify, write=True,
                    )
                return _run_execute(controller, name, args, _paste, verify_fn=verify, write=True)
            if args.get("confirm") is not True:
                raise MCPError("ren_paste_commit requires confirm true after the clipboard preview")
            receipt = controller.paste_current_draft()
            return {"status": "pasted", "outcome": receipt.outcome, "evidence": receipt.evidence}
        if name == "ren_browser":
            if args.get("work"):
                def _browser():
                    return controller.browser.act(
                        args.get("op"),
                        confirm=True,
                        url=args.get("url"),
                        selector=args.get("selector"),
                        text=args.get("text"),
                        x=args.get("x"),
                        y=args.get("y"),
                        delta_y=args.get("delta_y"),
                    )

                def verify():
                    return controller.agentwork.verify.browser_state()

                op = args.get("op")
                mutating = op in {"click", "type", "move", "scroll"}
                if args.get("action_id"):
                    return controller.agentwork.execute_after_approval(args["action_id"], _browser, verify)
                if mutating and args.get("confirm") is not True:
                    return _run_execute(
                        controller, name, args, _browser,
                        require_approval=True, verify_fn=verify, write=True,
                    )
                return _run_execute(controller, name, args, _browser, verify_fn=verify, write=mutating)
            return controller.browser.act(
                args.get("op"),
                confirm=bool(args.get("confirm")),
                approval_id=args.get("approval_id"),
                url=args.get("url"),
                selector=args.get("selector"),
                text=args.get("text"),
                x=args.get("x"),
                y=args.get("y"),
                delta_y=args.get("delta_y"),
            )
        if name == "ren_files":
            op = args.get("op")
            if op == "list":
                return controller.workspace.list_dir(args.get("path") or ".")
            if op == "read":
                return controller.workspace.read(args.get("path", ""))
            if op == "write":
                if args.get("work"):
                    def _write():
                        if args.get("approval_id"):
                            return controller.workspace.commit_write(args.get("approval_id"))
                        return controller.workspace.write(
                            args.get("path", ""), args.get("content", ""), confirm=True
                        )

                    def verify():
                        return controller.agentwork.verify.file_written(
                            args.get("path", ""), args.get("content")
                        )

                    if args.get("action_id"):
                        return controller.agentwork.execute_after_approval(args["action_id"], _write, verify)
                    if args.get("confirm") is not True and not args.get("approval_id"):
                        return _run_execute(
                            controller, name, args, _write,
                            require_approval=True, verify_fn=verify, write=True,
                        )
                    return _run_execute(controller, name, args, _write, verify_fn=verify, write=True)
                if args.get("approval_id"):
                    return controller.workspace.commit_write(args.get("approval_id"))
                return controller.workspace.write(
                    args.get("path", ""),
                    args.get("content", ""),
                    confirm=bool(args.get("confirm")),
                )
            raise MCPError("ren_files op must be list, read, or write")
        if name == "ren_set_mode":
            new_mode = controller.set_mode(args.get("mode", "elevate"))
            return {"status": "ok", "mode": new_mode}
        if name == "ren_agent_hello":
            agent = current_agent.get() or {}
            return controller.memory.upsert_agent(
                args.get("name") or agent.get("name", "agent"),
                args.get("capabilities"),
            )
        if name == "ren_memory_remember":
            return controller.memory.remember(
                args.get("key", ""), args.get("value", ""), args.get("tags", "")
            )
        if name == "ren_memory_recall":
            return {"facts": controller.memory.recall(args.get("query", ""), int(args.get("limit") or 5))}
        if name == "ren_memory_forget":
            return {"forgotten": controller.memory.forget(args.get("key", ""))}
        if name == "ren_handoff_create":
            agent = current_agent.get() or {}
            return controller.memory.create_handoff(
                args.get("task", ""), args.get("requires"), created_by=agent.get("name", "mcp")
            )
        if name == "ren_handoff_claim":
            agent = current_agent.get() or {}
            claimed = controller.memory.claim_handoff(
                args.get("agent") or agent.get("name", "agent"), args.get("capabilities")
            )
            return claimed or {"status": "no_open_handoffs"}
        if name == "ren_handoff_complete":
            return controller.memory.complete_handoff(
                int(args.get("id") or 0), args.get("result", ""), bool(args.get("ok", True))
            )
        if name == "ren_handoff_list":
            return {"handoffs": controller.memory.list_handoffs(args.get("status"))}
        if name == "ren_speak":
            text = (args.get("text") or "").strip()
            if not text:
                raise MCPError("ren_speak requires text")
            controller.speak_async(text)
            return {"status": "speaking", "chars": len(text)}
        if name == "ren_live_context":
            return _handle_live_context_local(controller, args)
        if name == "ren_live_converse":
            return _handle_live_converse_local(controller, args)
        if name == "ren_live_history":
            return _handle_live_history_local(controller, args)
        if name == "ren_work_begin":
            return _handle_work_begin(controller, args)
        if name == "ren_work_actions":
            return _handle_work_actions(controller, args)
        if name == "ren_work_status":
            return _handle_work_status(controller, args)
        if name == "ren_work_cancel":
            return _handle_work_cancel(controller, args)
        if name == "ren_work_poll":
            return _handle_work_poll(controller, args)
        if name == "ren_work_approvals":
            return _handle_work_approvals(controller, args)
        if name == "ren_work_resolve":
            return _handle_work_resolve(controller, args)
        if name == "ren_work_policy":
            return _handle_work_policy(controller, args)
        if name == "ren_work_route":
            return _handle_work_route(controller, args)
        if name == "ren_visual_query":
            return _handle_visual_query(controller, args)
        raise MCPError(f"Unknown tool {name}")
    except MCPError:
        raise
    except Exception as exc:
        raise MCPError(str(exc)) from exc


def _handle_live_context_http(args: dict) -> dict:
    include_screenshot = bool(args.get("include_screenshot", False))
    include_files = bool(args.get("include_files", True))
    include_windows = bool(args.get("include_windows", True))
    file_paths = args.get("file_paths") or []
    params = urllib.parse.urlencode(
        {
            "include_screenshot": str(include_screenshot).lower(),
            "include_files": str(include_files).lower(),
            "include_windows": str(include_windows).lower(),
            "files": ",".join(file_paths),
        }
    )
    try:
        return _request("GET", f"/api/live/context?{params}")
    except MCPError:
        from wren.core.live_context import LiveContextSnapshotter

        return LiveContextSnapshotter().capture(
            include_files=include_files,
            include_windows=include_windows,
            include_screenshot=include_screenshot,
            file_paths=file_paths or None,
        )


def _handle_live_converse_http(args: dict) -> dict:
    payload = {
        "text": args.get("text", ""),
        "role": args.get("role", "user"),
        "include_screenshot": bool(args.get("include_screenshot", False)),
        "include_files": bool(args.get("include_files", True)),
        "include_windows": bool(args.get("include_windows", True)),
        "file_paths": args.get("file_paths") or [],
    }
    try:
        return _request("POST", "/api/live/converse", payload)
    except MCPError:
        from wren.core.live_context import LiveConversationEngine

        return LiveConversationEngine().converse(
            payload["text"],
            role=payload["role"],
            include_screenshot=payload["include_screenshot"],
            include_files=payload["include_files"],
            include_windows=payload["include_windows"],
            file_paths=payload["file_paths"] or None,
        )


def _handle_live_history_http(args: dict) -> dict:
    op = args.get("op", "get")
    limit = int(args.get("limit", 30))
    if op == "clear":
        try:
            return _request("DELETE", "/api/live/history")
        except MCPError:
            from wren.core.live_context import LiveConversationEngine

            return LiveConversationEngine().clear_history()
    try:
        return _request("GET", f"/api/live/history?limit={limit}")
    except MCPError:
        from wren.core.live_context import LiveConversationEngine

        return {"turns": LiveConversationEngine().get_history(limit=limit)}


def dispatch(name: str, args: dict) -> dict:
    # Per-agent scope gate: tokens resolve to {'name','scopes'} upstream;
    # 'read'-scoped agents get read-only tools. None (stdio/local) = full.
    from wren.core.auth import tool_allowed

    agent = current_agent.get()
    if agent is not None and not tool_allowed(agent, name, args):
        raise MCPError(f"agent '{agent.get('name')}' scope denied tool {name}")
    # In-process MCP transport: run the tool directly against the live
    # controller. No HTTP call to the REST API, so a proxy (Cloudflare) can
    # never block the tools by banning the self-call.
    controller = local_controller.get()
    if controller is not None:
        return _dispatch_local(controller, name, args)
    # Standalone stdio bridge: separate process, so the loopback HTTP API is
    # the only way to reach the running Ren.
    if name == "ren_status":
        return _request("GET", "/api/status")
    if name == "ren_converse":
        return _request("POST", "/api/converse", {"text": args.get("text", "")})
    if name == "ren_dictate":
        return _request("POST", "/api/dictate", {"text": args.get("text", ""), "source": "mcp"})
    if name == "ren_screen_read":
        source = args.get("source") or "both"
        out: dict = {"source": source}
        if source in {"phone", "both"}:
            out["phone"] = _request("GET", "/api/screen/phone")
        if source in {"desktop", "both"}:
            out["desktop"] = _request("GET", "/api/screen/active")
        return out
    if name == "ren_paste_preview":
        return _request("GET", "/api/paste/preview")
    if name == "ren_paste_commit":
        if args.get("confirm") is not True:
            raise MCPError("ren_paste_commit requires confirm true after the clipboard preview")
        return _request("POST", "/api/paste", {})
    if name == "ren_browser":
        return _request("POST", "/api/browser/act", args)
    if name == "ren_files":
        op = args.get("op")
        if op == "list":
            path = urllib.parse.quote(args.get("path") or ".")
            return _request("GET", f"/api/workspace/list?path={path}")
        if op == "read":
            return _request("POST", "/api/workspace/read", {"path": args.get("path", "")})
        if op == "write":
            return _request(
                "POST",
                "/api/workspace/write",
                {
                    "path": args.get("path", ""),
                    "content": args.get("content", ""),
                    "confirm": bool(args.get("confirm")),
                    "approval_id": args.get("approval_id"),
                },
            )
        raise MCPError("ren_files op must be list, read, or write")
    if name == "ren_set_mode":
        return _request("POST", "/api/mode", {"mode": args.get("mode", "elevate")})
    if name == "ren_agent_hello":
        return _request("POST", "/api/agents/hello", args)
    if name == "ren_memory_remember":
        return _request("POST", "/api/memory/remember", args)
    if name == "ren_memory_recall":
        return _request(
            "GET",
            "/api/memory/recall?query=" + urllib.parse.quote(args.get("query", ""))
            + f"&limit={int(args.get('limit') or 5)}",
        )
    if name == "ren_memory_forget":
        return _request("POST", "/api/memory/forget", {"key": args.get("key", "")})
    if name == "ren_handoff_create":
        return _request("POST", "/api/handoffs", args)
    if name == "ren_handoff_claim":
        return _request("POST", "/api/handoffs/claim", args)
    if name == "ren_handoff_complete":
        return _request("POST", f"/api/handoffs/{int(args.get('id') or 0)}/complete", args)
    if name == "ren_handoff_list":
        qs = f"?status={args['status']}" if args.get("status") else ""
        return _request("GET", f"/api/handoffs{qs}")
    if name == "ren_speak":
        return _request("POST", "/api/speak", {"text": args.get("text", "")})
    if name == "ren_live_context":
        engine = _get_local_live_engine()
        return engine.snapshotter.capture(
            include_files=bool(args.get("include_files", True)),
            include_windows=bool(args.get("include_windows", True)),
            include_screenshot=bool(args.get("include_screenshot", False)),
            file_paths=args.get("file_paths") or None,
        )
    if name == "ren_live_converse":
        engine = _get_local_live_engine()
        return engine.converse(
            args.get("text", ""),
            role=args.get("role", "user"),
            include_screenshot=bool(args.get("include_screenshot", False)),
            include_files=bool(args.get("include_files", True)),
            include_windows=bool(args.get("include_windows", True)),
            file_paths=args.get("file_paths") or None,
        )
    if name == "ren_live_history":
        engine = _get_local_live_engine()
        op = args.get("op", "get")
        limit = int(args.get("limit", 30))
        if op == "clear":
            return engine.clear_history()
        return {"turns": engine.get_history(limit=limit)}
    if name.startswith("ren_work_") or name == "ren_visual_query":
        raise MCPError(
            f"{name} requires the live HTTP MCP transport (in-process controller); "
            "stdio bridge has no controller to drive the agent-work ledger."
        )
    raise MCPError(f"Unknown tool {name}")


def handle(message: dict) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method and method.startswith("notifications/"):
        return None
    if msg_id is None and method:
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def fail(text: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": text}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ren", "version": "0.1.0a3"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            result = dispatch(params.get("name", ""), params.get("arguments") or {})
            return ok(
                {"content": _format_tool_content(result), "isError": False}
            )
        except MCPError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if method is None and msg_id is not None:
        return fail("Missing method")
    return fail(f"Unsupported method {method}")


def _format_tool_content(result: dict) -> list[dict]:
    """Build MCP content blocks, separating any PNG screenshot into a native image block."""
    payload = dict(result)
    screenshot_b64 = payload.pop("screenshot_b64", "")
    blocks: list[dict] = [{"type": "text", "text": json.dumps(payload)}]
    if screenshot_b64:
        blocks.append({"type": "image", "data": screenshot_b64, "mimeType": "image/png"})
    return blocks


def main() -> None:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
