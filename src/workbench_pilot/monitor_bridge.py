"""A fixed, authenticated loopback bridge; never an arbitrary process or stdin API."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .process_monitor import OwnedProcessMonitor


def checked_origin(origin: str) -> str:
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", origin or "")
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ValueError("Monitor bridge must use an exact 127.0.0.1 HTTP origin with an explicit port")
    return origin


class MonitorBridge:
    def __init__(self, monitor: OwnedProcessMonitor, ren_origin: str):
        self.monitor = monitor
        self.ren_origin = checked_origin(ren_origin)
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.thread = None
        self.origin = ""

    def start(self) -> str:
        if self.server is not None:
            return self.origin
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_args):
                pass  # Never log authentication material or captured child output here.

            def respond(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self):
                if self.headers.get("Host") != bridge.origin.removeprefix("http://"):
                    self.respond(400, {"error": "Invalid loopback host"})
                    return False
                if self.headers.get("Origin") is not None:
                    self.respond(403, {"error": "Browser-origin requests are not accepted by the bridge"})
                    return False
                supplied = self.headers.get("X-Ren-Monitor-Token", "")
                if (
                    not supplied.isascii()
                    or len(supplied) > 256
                    or not hmac.compare_digest(supplied, bridge.token)
                ):
                    self.respond(403, {"error": "Monitor authentication failed"})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path != "/snapshot":
                    return self.respond(404, {"error": "Unknown monitor route"})
                return self.respond(
                    200,
                    {
                        **bridge.monitor.snapshot(),
                        "ren_origin": bridge.ren_origin,
                        "mode": "G1 offline",
                        "live_services": "disabled and unverified",
                        "refresh_seconds": 0.5,
                    },
                )

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path not in {"/start", "/stop", "/clear"}:
                    return self.respond(404, {"error": "Unknown monitor command"})
                if self.headers.get("Transfer-Encoding") is not None:
                    return self.respond(400, {"error": "Chunked commands are not accepted"})
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    return self.respond(415, {"error": "JSON is required"})
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if not 0 < length <= 256:
                        return self.respond(413, {"error": "Command body must be a small empty object"})
                    payload = json.loads(self.rfile.read(length))
                    if type(payload) is not dict or payload:
                        return self.respond(422, {"error": "Commands take no arguments"})
                except (ValueError, UnicodeError, TimeoutError):
                    return self.respond(400, {"error": "Invalid command body"})
                if self.path == "/clear":
                    bridge.monitor.clear()
                else:
                    future = bridge.monitor.start() if self.path == "/start" else bridge.monitor.stop()
                    if future.done() and future.exception() is not None:
                        return self.respond(409, {"error": "Process operation could not be scheduled"})
                # Accepted is not a claim that startup/shutdown has completed.
                return self.respond(202, {"accepted": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="ren-monitor-bridge", daemon=True
        )
        try:
            self.thread.start()
        except BaseException:
            # shutdown() must not be called before serve_forever() starts: it
            # waits for a server-loop event that would never be set.
            self.server.server_close()
            self.server = None
            self.thread = None
            self.origin = ""
            raise
        return self.origin

    def close(self):
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=3)
                if self.thread.is_alive():
                    raise RuntimeError("Monitor bridge thread did not stop")
            self.server = None
            self.thread = None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None  # Never forward a local control capability to a redirected destination.


class BridgeClient:
    def __init__(self, origin: str, token: str):
        self.origin = checked_origin(origin)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError("A local monitor capability is required")
        self._token = token
        # Never send local capabilities through inherited HTTP proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _request(self, path: str, post=False):
        request = urllib.request.Request(
            self.origin + path,
            data=b"{}" if post else None,
            headers={"X-Ren-Monitor-Token": self._token, "Content-Type": "application/json"},
            method="POST" if post else "GET",
        )
        try:
            with self._opener.open(request, timeout=2) as response:
                payload = response.read(512001)
                if len(payload) > 512000:
                    raise ValueError("Oversized monitor snapshot")
                return json.loads(payload)
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            # Do not include request headers, the token, or an arbitrary response body.
            raise RuntimeError("Local monitor bridge is unavailable or rejected the request") from exc

    def snapshot(self):
        return self._request("/snapshot")

    def command(self, action: str):
        if action not in {"start", "stop", "clear"}:
            raise ValueError("Only start, stop and clear are supported")
        return self._request("/" + action, post=True)
