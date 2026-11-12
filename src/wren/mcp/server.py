"""Stdio MCP bridge so other agents can call the running Ren workstation.

Ren itself must already be running (`wren`). This process only forwards tool
calls to the loopback API. It does not open its own browser or microphone.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2024-11-05"
REN_URL = os.environ.get("REN_URL", "http://127.0.0.1:8765").rstrip("/")

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
    req = urllib.request.Request(
        REN_URL + path,
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


def dispatch(name: str, args: dict) -> dict:
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
