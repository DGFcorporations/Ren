"""Streamable-HTTP MCP transport for the Ren tool catalog.

Serves the same tools as the stdio ``ren-mcp`` bridge at ``/mcp`` inside the
Wren FastAPI app, so remote MCP clients (cloud agents via a future public
ingress, LAN/Tailscale agents today) can drive the running workstation.
Tool semantics, confirm gates, and error mapping are identical to stdio —
this module is transport only and never reimplements ``dispatch``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool

from wren.mcp import server as bridge
from wren.mcp.server import TOOLS

logger = logging.getLogger(__name__)


def _base_url_from_scope(scope: dict) -> str:
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    host = headers.get("host", "")
    if not host:
        server = scope.get("server") or ("127.0.0.1", 8765)
        host = f"{server[0]}:{server[1]}"
    scheme = scope.get("scheme") or "http"
    return f"{scheme}://{host}"


class McpHttpTransport:
    """ASGI endpoint + session manager exposing the Ren tool catalog over MCP.

    Created inside the Ren server process, so it holds the live controller:
    tool calls run directly against it (``wren.mcp.server.local_controller``)
    instead of HTTP-calling the REST API. A remote MCP client behind a proxy
    therefore cannot break the tools by blocking the self-call.
    """

    def __init__(self, controller=None) -> None:
        self.controller = controller
        self.server = Server("ren", version="0.1.0a3")

        @self.server.list_tools()
        async def _list_tools() -> list[Tool]:
            return [
                Tool(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    inputSchema=tool.get("inputSchema", {"type": "object"}),
                )
                for tool in TOOLS
            ]

        @self.server.call_tool()
        async def _call_tool(name: str, arguments: dict) -> CallToolResult:
            try:
                # dispatch() is sync. When this transport holds the live
                # controller it runs the tool directly (no HTTP self-call);
                # the stdio bridge falls back to the loopback API. Either
                # way it must not run on the event loop, or it deadlocks.
                result = await asyncio.to_thread(bridge.dispatch, name, arguments or {})
            except bridge.MCPError as exc:
                return CallToolResult(
                    content=[TextContent(type="text", text=str(exc))], isError=True
                )
            except Exception as exc:
                logger.warning("ren-mcp tool %s failed: %s", name, exc)
                return CallToolResult(
                    content=[
                        TextContent(type="text", text=f"{type(exc).__name__}: {exc}")
                    ],
                    isError=True,
                )
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result))],
                isError=False,
            )

        self.manager = StreamableHTTPSessionManager(app=self.server, json_response=True)

    async def __call__(self, scope: dict, receive, send) -> None:
        token = bridge.request_base_url.set(_base_url_from_scope(scope))
        # Register the live controller so dispatch() runs tools directly
        # instead of HTTP-calling the REST API. Reset after the request so
        # the stdio bridge (same module, separate process) is unaffected.
        local_token = bridge.local_controller.set(self.controller)
        # Each MCP request extends the remote-agent lease: while an agent is
        # connected, local mutating surfaces (hotkeys, local REST actions)
        # yield so the agent runs uncontested. Lease expires on its own when
        # the agent goes quiet — no disconnect handshake required.
        touch = getattr(self.controller, "touch_remote_session", None)
        if touch is not None:
            touch()
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            bridge.local_controller.reset(local_token)
            bridge.request_base_url.reset(token)


def create_mcp_transport(controller=None) -> McpHttpTransport | None:
    """Build the HTTP MCP transport, or None when the ``mcp`` package is absent.

    ``controller`` is the live WrenController from ``create_app``; when given,
    MCP tool calls run directly against it instead of HTTP-calling the API.
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        logger.warning("mcp package not installed; /mcp HTTP transport disabled")
        return None
    return McpHttpTransport(controller)
