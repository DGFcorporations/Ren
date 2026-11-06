"""Loopback-only synthetic pilot UI. No microphone, provider, or real-account endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .adapters import LiveIntegrationDisabled, fixture_catalogue, require_live_integration, synthetic_tone
from .browser import BrowserUnavailable, FixtureBrowser, FixtureViolation
from .controller import OWNER, OfflinePilot
from .ledger import Conflict, Ledger, LedgerError, NotFound
from .policy import InstanceLock, PilotSettings

STATIC = Path(__file__).parent / "static"
COOKIE = "g1_local_session"


class LimitedBodies:
    """Bound POST bytes including chunked bodies before JSON parsing."""

    def __init__(self, app, max_bytes=65536):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        parts, size = [], 0
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=5)
            except TimeoutError:
                return await JSONResponse({"detail": "Request body timed out"}, status_code=408)(
                    scope, receive, send
                )
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                return await JSONResponse({"detail": "Body exceeds 64 KiB"}, status_code=413)(
                    scope, receive, send
                )
            parts.append(chunk)
            if not message.get("more_body", False):
                break
        used = False

        async def replay():
            nonlocal used
            if not used:
                used = True
                return {"type": "http.request", "body": b"".join(parts), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartBody(StrictBody):
    request_id: str = Field(min_length=1, max_length=100)
    fixture_id: Literal["kickoff", "checklist", "unicode"]
    surface: Literal["textarea", "contenteditable", "iframe", "delayed"] = "textarea"


class DecisionBody(StrictBody):
    approved: StrictBool
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemoryBody(StrictBody):
    request_id: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=11, max_length=120, pattern=r"^synthetic\.")
    value: str | None = Field(default=None, max_length=4000)
    delete: StrictBool = False


class FaultBody(StrictBody):
    kind: Literal["recipient", "draft", "reload", "close", "memory"]


def create_app(settings: PilotSettings, *, browser_factory=FixtureBrowser, event_sink=None) -> FastAPI:
    # These are local anti-CSRF/session capabilities, not external service credentials.
    csrf_secret = secrets.token_bytes(32)
    sessions: OrderedDict[str, None] = OrderedDict()
    sessions_lock = threading.RLock()
    instance_lock = InstanceLock(settings.data_dir / "instance.lock")

    def csrf_for(session: str) -> str:
        return hmac.new(csrf_secret, session.encode(), hashlib.sha256).hexdigest()

    @asynccontextmanager
    async def lifespan(app):
        instance_lock.acquire()
        ledger = browser = pilot = None
        try:
            ledger = Ledger(settings.data_dir / "ledger.sqlite3")
            browser = browser_factory(settings.origin)
            pilot = OfflinePilot(settings, ledger, browser, event_sink=event_sink)
            app.state.pilot = pilot
            yield
        finally:
            if pilot:
                await asyncio.to_thread(pilot.close)
            elif browser:
                await asyncio.to_thread(browser.close)
            if ledger:
                ledger.close()
            instance_lock.close()

    app = FastAPI(
        title="Workbench G1 Offline Pilot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.add_middleware(LimitedBodies)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        actual_origin = f"{request.url.scheme}://{request.url.netloc}"
        if actual_origin != settings.origin:
            return JSONResponse({"detail": "Only the configured loopback origin is allowed"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and origin != settings.origin:
            return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site requests are disabled"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        frame_policy = "'self'" if request.url.path.startswith("/fixtures/") else "'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; "
            f"base-uri 'none'; frame-ancestors {frame_policy}"
        )
        response.headers["Permissions-Policy"] = "microphone=(), camera=(), geolocation=()"
        return response

    def local_session(request: Request) -> str:
        session = request.cookies.get(COOKIE, "")
        with sessions_lock:
            known_session = session in sessions
        if not known_session:
            raise HTTPException(401, "Open the local pilot UI to create a synthetic session")
        if request.method not in {"GET", "HEAD"}:
            if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                raise HTTPException(415, "JSON requests are required")
            token = request.headers.get("x-pilot-csrf", "")
            if not hmac.compare_digest(token, csrf_for(session)):
                raise HTTPException(403, "Missing or invalid anti-CSRF token")
        return session

    session_dependency = Depends(local_session)

    @app.exception_handler(LedgerError)
    async def ledger_error(request, exc):
        code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 422
        return JSONResponse({"detail": str(exc)}, status_code=code)

    @app.exception_handler(FixtureViolation)
    async def fixture_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(BrowserUnavailable)
    async def browser_error(request, exc):
        return JSONResponse({"detail": "Sandboxed browser unavailable; no fallback used"}, status_code=503)

    @app.exception_handler(LiveIntegrationDisabled)
    async def live_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        session = request.cookies.get(COOKIE, "")
        evicted = []
        with sessions_lock:
            if session not in sessions:
                session = secrets.token_hex(24)
                sessions[session] = None
                while len(sessions) > 32:
                    evicted.append(sessions.popitem(last=False)[0])
        for old_session in evicted:
            app.state.pilot.abandon_session(old_session)
        html = (STATIC / "pilot.html").read_text(encoding="utf-8")
        response = HTMLResponse(html.replace("{{csrf_token}}", csrf_for(session)))
        response.set_cookie(COOKIE, session, httponly=True, samesite="strict", max_age=3600)
        return response

    @app.get("/assets/{name}")
    def assets(name: str):
        if name not in {"pilot.js", "pilot.css"}:
            raise HTTPException(404, "Unknown UI asset")
        return FileResponse(STATIC / name)

    @app.get("/fixtures/draftbox", response_class=HTMLResponse)
    def draftbox():
        return (STATIC / "draftbox.html").read_text(encoding="utf-8")

    @app.get("/fixtures/assets/{name}")
    def fixture_asset(name: str):
        if name not in {"draftbox.js", "draftbox.css", "frame.html"}:
            raise HTTPException(404, "Unknown fixture asset")
        return FileResponse(STATIC / name)

    @app.get("/api/pilot/status")
    def status(session=session_dependency):
        return app.state.pilot.describe()

    @app.get("/api/pilot/fixtures")
    def fixtures(session=session_dependency):
        return fixture_catalogue()

    @app.get("/api/pilot/facts")
    def facts(q: str = "", session=session_dependency):
        if len(q) > 1000:
            raise HTTPException(422, "Query is too long")
        ledger = app.state.pilot.ledger
        rows = ledger.search_facts(OWNER, q, 20) if q else ledger.list_facts(OWNER)[:50]
        return {"backend": ledger.search_backend, "facts": rows, "synthetic": True}

    @app.post("/api/pilot/runs")
    def start(body: StartBody, session=session_dependency):
        return app.state.pilot.start_fixture(session, body.request_id, body.fixture_id, body.surface)

    @app.post("/api/pilot/memory/proposals")
    def memory(body: MemoryBody, session=session_dependency):
        if not body.delete and body.value is None:
            raise HTTPException(422, "A synthetic value is required")
        return app.state.pilot.propose_memory(
            session, body.request_id, body.key, body.value, delete=body.delete
        )

    def checked_id(value):
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise HTTPException(404, "Unknown synthetic identifier")
        return value

    @app.get("/api/pilot/runs/{run_id}")
    def run(run_id: str, session=session_dependency):
        return app.state.pilot.get_run(session, checked_id(run_id))

    @app.post("/api/pilot/approvals/{action_id}")
    def approve(action_id: str, body: DecisionBody, session=session_dependency):
        return app.state.pilot.decide(session, checked_id(action_id), body.approved, body.digest)

    @app.post("/api/pilot/runs/{run_id}/cancel")
    def cancel(run_id: str, session=session_dependency):
        return app.state.pilot.cancel(session, checked_id(run_id))

    @app.post("/api/pilot/runs/{run_id}/fault")
    def fault(run_id: str, body: FaultBody, session=session_dependency):
        return app.state.pilot.fault(session, checked_id(run_id), body.kind)

    @app.get("/api/pilot/runs/{run_id}/events")
    def events(run_id: str, session=session_dependency):
        return app.state.pilot.events(session, checked_id(run_id))

    @app.get("/api/pilot/runs/{run_id}/preview.png")
    def preview(run_id: str, session=session_dependency):
        image = app.state.pilot.screenshot(session, checked_id(run_id))
        return Response(image, media_type="image/png")

    @app.get("/api/pilot/tone.wav")
    def tone(session=session_dependency):
        return Response(synthetic_tone(), media_type="audio/wav")

    @app.post("/api/pilot/live/{name}")
    def live(name: str, session=session_dependency):
        require_live_integration(name)

    return app
