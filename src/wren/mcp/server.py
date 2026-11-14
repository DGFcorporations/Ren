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
        "description": "Paste the current preview into the focused workstation window. Requires confirm true.",
        "inputSchema": {
            "type": "object",
            "properties": {"confirm": {"type": "boolean"}},
            "required": ["confirm"],
        },
    },
    {
        "name": "ren_browser",
        "description": "Control Ren's owned Chromium. snapshot and navigate run immediately. click, type, move, and scroll require confirm true.",
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
]


class MCPError(RuntimeError):
    pass


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
        raise MCPError(f"Ren is not reachable at {REN_URL}. Start it with ./run_ren.sh. {exc.reason}") from exc


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
            if args.get("confirm") is not True:
                raise MCPError("ren_paste_commit requires confirm true after the clipboard preview")
            receipt = controller.paste_current_draft()
            return {"status": "pasted", "outcome": receipt.outcome, "evidence": receipt.evidence}
        if name == "ren_browser":
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
        raise MCPError(f"Unknown tool {name}")
    except MCPError:
        raise
    except Exception as exc:
        raise MCPError(str(exc)) from exc


def dispatch(name: str, args: dict) -> dict:
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
            return ok({"content": [{"type": "text", "text": json.dumps(result)}], "isError": False})
        except MCPError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if method is None and msg_id is not None:
        return fail("Missing method")
    return fail(f"Unsupported method {method}")


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
