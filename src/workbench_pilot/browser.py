"""Real, anonymous Playwright automation for the owned G1 fixture only.

No caller-supplied URLs, selectors, JavaScript, profiles, or browser launch flags
are accepted. Every Playwright operation (including teardown and event handlers)
lives on one executor thread. This module intentionally has no simulated-browser
fallback: an unavailable sandboxed Chromium is an unavailable integration.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit


class BrowserUnavailable(RuntimeError):
    """Playwright or a securely launched Chromium is unavailable."""


class FixtureViolation(ValueError):
    """An argument or browser state falls outside the owned fixture boundary."""


_SURFACES = frozenset({"textarea", "contenteditable", "iframe", "delayed"})
_TAMPERS = frozenset({"recipient", "draft", "reload", "close"})
_ASSET_PATHS = frozenset(
    {
        "/fixtures/assets/draftbox.js",
        "/fixtures/assets/draftbox.css",
        "/fixtures/assets/frame.html",
    }
)
_MAX_RUNS = 5
_TIMEOUT_MS = 7_000
_TAMPER_TEXT = "Synthetic preexisting draft (controlled fixture tamper)."
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "frame-src 'self'; frame-ancestors 'self'; img-src data:; "
    "connect-src 'none'; worker-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)


def _parse_origin(origin: str) -> tuple[str, int]:
    """Accept only a canonical, explicit-port loopback HTTP origin.

    Parsing alone is insufficient: browsers normalize controls, backslashes,
    abbreviated/numeric IPs, credentials, and default ports differently. Reject
    ambiguous spellings before parsing and never resolve arbitrary DNS names.
    """
    if not isinstance(origin, str):
        raise FixtureViolation("origin must be a loopback HTTP origin string")
    match = re.fullmatch(r"http://(localhost|[0-9.]+|\[::1\]):([1-9][0-9]{0,4})", origin)
    if match is None:
        raise FixtureViolation("Use an exact loopback http://host:numeric-port origin without a path")
    host, raw_port = match.groups()
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise FixtureViolation("origin port must be between 1 and 65535")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError as exc:
            raise FixtureViolation("origin must use a canonical loopback address") from exc
        if not address.is_loopback or str(address) != host.strip("[]"):
            raise FixtureViolation("origin must use a canonical loopback address")
    return host, port


def _validate_run_id(run_id: str) -> None:
    if (
        not isinstance(run_id, str)
        or not run_id
        or len(run_id) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in run_id)
    ):
        raise FixtureViolation("run_id must be a nonempty synthetic identifier of at most 128 characters")


@dataclass
class _Run:
    run_id: str
    surface: str
    context: Any
    page: Any = None
    violation: str | None = None
    primary_registered: bool = False
    closing: bool = False


class FixtureBrowser:
    """A blocking facade over a single, thread-affine Playwright worker."""

    def __init__(self, origin: str, *, headless: bool = True):
        self._host, self._port = _parse_origin(origin)
        if not isinstance(headless, bool):
            raise FixtureViolation("headless must be a boolean")
        self.origin = origin
        self._headless = headless
        self._authority = f"{self._host}:{self._port}"
        self._gate = threading.Lock()
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="g1-fixture-browser")
        # The following state is used exclusively by the executor thread.
        self._playwright: Any = None
        self._browser: Any = None
        self._expect: Any = None
        self._runs: dict[str, _Run] = {}

    def open(self, run_id: str, surface: str = "textarea") -> dict:
        _validate_run_id(run_id)
        if not isinstance(surface, str) or surface not in _SURFACES:
            raise FixtureViolation("surface must be textarea, contenteditable, iframe, or delayed")
        return self._call(self._open, run_id, surface)

    def snapshot(self, run_id: str) -> dict:
        _validate_run_id(run_id)
        return self._call(self._snapshot, run_id)

    def execute(self, run_id: str, text: str, *, expected_snapshot: dict | None = None) -> dict:
        _validate_run_id(run_id)
        if not isinstance(text, str) or len(text) > 8000:
            raise FixtureViolation("draft text must be a string of at most 8000 characters")
        if expected_snapshot is not None and not isinstance(expected_snapshot, dict):
            raise FixtureViolation("expected_snapshot must be a complete fixture snapshot")
        return self._call(self._execute, run_id, text, deepcopy(expected_snapshot))

    def tamper(self, run_id: str, kind: str) -> dict:
        _validate_run_id(run_id)
        if not isinstance(kind, str) or kind not in _TAMPERS:
            raise FixtureViolation("Only the controlled recipient, draft, reload, and close tampers exist")
        return self._call(self._tamper, run_id, kind)

    def screenshot(self, run_id: str) -> bytes:
        _validate_run_id(run_id)
        return self._call(self._screenshot, run_id)

    def close_run(self, run_id: str) -> None:
        _validate_run_id(run_id)
        self._call(self._close_run, run_id)

    def close(self) -> None:
        with self._gate:
            if self._closed:
                return
            self._closed = True
            future = self._executor.submit(self._close_all)
        try:
            future.result()
        finally:
            self._executor.shutdown(wait=True)

    def _call(self, operation: Any, *args: Any) -> Any:
        with self._gate:
            if self._closed:
                raise FixtureViolation("fixture browser is closed")
            future = self._executor.submit(operation, *args)
        return future.result()

    def _ensure_started(self) -> None:
        if self._browser is not None:
            if not self._browser.is_connected():
                raise BrowserUnavailable(
                    "The isolated Chromium worker has disconnected; no fallback was used"
                )
            return
        try:
            from playwright.sync_api import expect, sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed; real browser execution is unavailable"
            ) from exc
        try:
            self._playwright = sync_playwright().start()
            # Explicitly keep Chromium's sandbox enabled. In particular, do not
            # retry with --no-sandbox, a personal profile, or a remote browser.
            self._browser = self._playwright.chromium.launch(
                headless=self._headless,
                chromium_sandbox=True,
            )
            self._expect = expect
        except Exception as exc:
            if self._playwright is not None:
                with suppress(Exception):
                    self._playwright.stop()
            self._playwright = None
            self._browser = None
            raise BrowserUnavailable(
                "Sandboxed Chromium could not launch; no insecure or simulated fallback was attempted. "
                + str(exc)[:1800]
            ) from exc

    def _query(self, run_id: str, surface: str) -> str:
        return urlencode({"run_id": run_id, "surface": surface})

    def _url_allowed(self, url: str, run_id: str, surface: str, *, main: bool = False) -> bool:
        """Strict URL policy shared by routing and post-navigation verification."""
        if (
            not isinstance(url, str)
            or not url.isascii()
            or "\\" in url
            or any(ord(character) <= 32 or ord(character) == 127 for character in url)
        ):
            return False
        try:
            parts = urlsplit(url)
            # Chromium removes :80 from serialized HTTP URLs. This is the same
            # origin, not a second allowed port or a second allowed hostname.
            authorities = {self._authority}
            if self._port == 80:
                authorities.add(self._host)
            if (
                parts.scheme != "http"
                or parts.netloc not in authorities
                or parts.username is not None
                or parts.password is not None
                or (parts.port or 80) != self._port
                or parts.fragment
                or "#" in url
            ):
                return False
            if parts.path == "/fixtures/draftbox":
                return parts.query == self._query(run_id, surface)
            return not main and parts.path in _ASSET_PATHS and not parts.query and "?" not in url
        except (ValueError, UnicodeError):
            return False

    @staticmethod
    def _mark_violation(run: _Run, reason: str) -> None:
        if not run.closing and run.violation is None:
            run.violation = reason

    def _register_page(self, run: _Run, page: Any) -> None:
        # This is installed on the context BEFORE new_page and goto. Context
        # routing also covers a popup's first request, before its page event.
        if run.primary_registered:
            self._mark_violation(run, "New windows/popups are forbidden in the fixture worker")
            with suppress(Exception):
                page.close()
            return
        run.primary_registered = True
        run.page = page
        page.on("framenavigated", lambda frame: self._navigated(run, frame))
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("download", lambda download: self._downloaded(run, download))

    def _downloaded(self, run: _Run, download: Any) -> None:
        self._mark_violation(run, "Downloads are forbidden in the fixture worker")
        with suppress(Exception):
            download.cancel()

    def _navigated(self, run: _Run, frame: Any) -> None:
        if run.closing:
            return
        is_main = frame == run.page.main_frame
        allowed = self._url_allowed(frame.url, run.run_id, run.surface, main=is_main)
        if not is_main:
            allowed = (
                allowed
                and run.surface == "iframe"
                and urlsplit(frame.url).path == "/fixtures/assets/frame.html"
            )
        if not allowed:
            self._mark_violation(run, "Navigation left the exact owned fixture page/frame")
            with suppress(Exception):
                run.page.close()

    def _route(self, run: _Run, route: Any, request: Any) -> None:
        if run.closing:
            route.abort("blockedbyclient")
            return
        allowed = request.method == "GET" and self._url_allowed(request.url, run.run_id, run.surface)
        try:
            frame = request.frame
            allowed = allowed and run.page is not None and frame.page == run.page
            if request.is_navigation_request():
                if frame == run.page.main_frame:
                    allowed = allowed and self._url_allowed(request.url, run.run_id, run.surface, main=True)
                else:
                    allowed = (
                        allowed
                        and run.surface == "iframe"
                        and urlsplit(request.url).path == "/fixtures/assets/frame.html"
                    )
            elif request.resource_type == "script":
                allowed = (
                    allowed
                    and frame == run.page.main_frame
                    and urlsplit(request.url).path == "/fixtures/assets/draftbox.js"
                )
            elif request.resource_type == "stylesheet":
                allowed = allowed and urlsplit(request.url).path == "/fixtures/assets/draftbox.css"
            else:
                allowed = False
        except Exception:
            allowed = False
        if not allowed:
            self._mark_violation(run, "Blocked a request outside the exact owned fixture allowlist")
            route.abort("blockedbyclient")
            return
        response = None
        try:
            # continue_() is deliberately not used: a redirected request may
            # bypass a second route callback. Never follow even a local redirect.
            response = route.fetch(max_redirects=0, max_retries=0, timeout=_TIMEOUT_MS)
            if 300 <= response.status < 400 or "refresh" in response.headers:
                self._mark_violation(run, "Fixture HTTP redirects/refresh responses are forbidden")
                route.abort("blockedbyclient")
                return
            if response.status != 200:
                self._mark_violation(run, "Owned fixture returned a non-success response")
                route.abort("failed")
                return
            headers = dict(response.headers)
            headers["content-security-policy"] = _CSP
            headers["cache-control"] = "no-store"
            headers["x-content-type-options"] = "nosniff"
            route.fulfill(response=response, headers=headers)
        except Exception:
            self._mark_violation(run, "Owned fixture request failed; no alternate network route was used")
            with suppress(Exception):
                route.abort("failed")
        finally:
            if response is not None:
                with suppress(Exception):
                    response.dispose()

    def _websocket(self, run: _Run, _socket: Any) -> None:
        self._mark_violation(run, "WebSockets are forbidden in the fixture worker")
        # A routed WebSocket has NO server connection unless connect_to_server
        # is explicitly called. Never call it. Let normal run cleanup close the
        # context outside this callback: a re-entrant sync socket.close() from
        # Playwright's socket dispatcher can deadlock the worker.

    def _open(self, run_id: str, surface: str) -> dict:
        if run_id in self._runs:
            if self._runs[run_id].surface != surface:
                raise FixtureViolation("An existing run cannot silently change its surface")
            return self._snapshot(run_id)
        if len(self._runs) >= _MAX_RUNS:
            raise FixtureViolation("At most five fixture runs may be open; close a terminal run first")
        self._ensure_started()
        context = self._browser.new_context(
            accept_downloads=False,
            service_workers="block",
            permissions=[],
            ignore_https_errors=False,
            viewport={"width": 1100, "height": 850},
        )
        run = _Run(run_id=run_id, surface=surface, context=context)
        self._runs[run_id] = run
        try:
            context.set_default_timeout(_TIMEOUT_MS)
            context.set_default_navigation_timeout(_TIMEOUT_MS)
            context.route("**/*", lambda route, request: self._route(run, route, request))
            context.route_web_socket("**/*", lambda socket: self._websocket(run, socket))
            context.on("page", lambda page: self._register_page(run, page))
            page = context.new_page()
            if run.page != page:
                raise FixtureViolation("Primary fixture page was not registered before navigation")
            page.goto(
                f"{self.origin}/fixtures/draftbox?{self._query(run_id, surface)}",
                wait_until="domcontentloaded",
            )
            return self._snapshot(run_id)
        except Exception as exc:
            reason = run.violation
            self._close_run(run_id)
            if reason:
                raise FixtureViolation(reason) from exc
            raise

    def _require_run(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise FixtureViolation("Unknown or closed fixture run")
        if run.violation:
            raise FixtureViolation(run.violation)
        if run.page is None or run.page.is_closed() or run.page not in run.context.pages:
            raise FixtureViolation("Fixture page/context is closed or no longer belongs to this run")
        if not self._url_allowed(run.page.url, run_id, run.surface, main=True):
            raise FixtureViolation("Fixture page URL no longer matches this run")
        return run

    @staticmethod
    def _composer(run: _Run) -> Any:
        if run.surface == "iframe":
            return run.page.frame_locator('[data-testid="composer-frame"]').get_by_test_id("composer")
        return run.page.get_by_test_id("composer")

    def _snapshot(self, run_id: str) -> dict:
        run = self._require_run(run_id)
        page = run.page
        page.locator('[data-testid="draftbox"][data-ready="true"]').wait_for(state="visible")
        root = page.get_by_test_id("draftbox")
        if root.count() != 1 or root.get_attribute("data-run-id") != run_id:
            raise FixtureViolation("Fixture document identity does not match this run")
        if root.get_attribute("data-surface") != run.surface:
            raise FixtureViolation("Fixture surface does not match this run")
        expected_frames = 2 if run.surface == "iframe" else 1
        if len(page.frames) != expected_frames:
            raise FixtureViolation("Unexpected fixture frame count")
        for frame in page.frames:
            if frame != page.main_frame and (
                not self._url_allowed(frame.url, run_id, run.surface)
                or urlsplit(frame.url).path != "/fixtures/assets/frame.html"
            ):
                raise FixtureViolation("Fixture iframe left its owned URL")
        epoch = root.get_attribute("data-page-epoch")
        if not epoch or re.fullmatch(r"[0-9a-f-]{36}", epoch) is None:
            raise FixtureViolation("Fixture page epoch is absent or malformed")
        account = page.get_by_test_id("account").inner_text()
        recipient = page.get_by_test_id("recipient").input_value()
        if account != "synthetic-account-a" or recipient not in {
            "synthetic-recipient-a",
            "synthetic-recipient-b",
        }:
            raise FixtureViolation("Fixture account/recipient is not synthetic")
        composer = self._composer(run)
        before_text = composer.inner_text() if run.surface == "contenteditable" else composer.input_value()
        raw_count = page.get_by_test_id("save-count").inner_text()
        if re.fullmatch(r"0|[1-9][0-9]{0,8}", raw_count) is None:
            raise FixtureViolation("Fixture save counter is malformed")
        # Recheck after the DOM reads: navigations/popups can be delivered while
        # Playwright is servicing any one of those blocking calls.
        self._require_run(run_id)
        return {
            "origin": self.origin,
            "tab_id": run_id,
            "account": account,
            "recipient": recipient,
            "page_epoch": epoch,
            "surface": run.surface,
            "before_text": before_text,
            "save_count": int(raw_count),
        }

    def _execute(self, run_id: str, text: str, expected_snapshot: dict | None = None) -> dict:
        before = self._snapshot(run_id)
        if expected_snapshot is not None and before != expected_snapshot:
            raise FixtureViolation("Approved fixture snapshot changed; no text was entered")
        run = self._require_run(run_id)
        self._composer(run).fill(text)
        filled = self._snapshot(run_id)
        if filled != {**before, "before_text": text}:
            raise FixtureViolation("Fixture context or composer changed before Save draft")
        armed = run.page.evaluate(
            "grant => window.G1DraftBox?.armSave(grant) === true", {**before, "text": text}
        )
        if not armed:
            raise FixtureViolation("Owned fixture could not bind the exact approved save")
        run.page.get_by_test_id("save-draft").click()
        if run.page.get_by_test_id("draftbox").get_attribute("data-save-rejected") == "true":
            raise FixtureViolation(
                "Owned fixture rejected an event-time target/content change; no save occurred"
            )
        # Never manufacture a receipt from the requested text or a successful
        # click. Read the separate persisted-in-page receipt and counter, then
        # recheck the live context. Any exception here reaches the controller,
        # which must treat a possible write as uncertain rather than retry it.
        self._expect(run.page.get_by_test_id("save-count")).to_have_text(str(before["save_count"] + 1))
        content = run.page.get_by_test_id("saved-content").text_content()
        recipient = run.page.get_by_test_id("saved-recipient").text_content()
        account = run.page.get_by_test_id("saved-account").text_content()
        after = self._snapshot(run_id)
        if (
            content != text
            or recipient != before["recipient"]
            or account != before["account"]
            or after != {**before, "before_text": text, "save_count": before["save_count"] + 1}
        ):
            raise FixtureViolation("Save draft may have occurred, but its independent receipt did not verify")
        return {
            "content": content,
            "recipient": recipient,
            "account": account,
            "save_count": after["save_count"],
            "verified": True,
            "origin": self.origin,
            "tab_id": run_id,
        }

    def _tamper(self, run_id: str, kind: str) -> dict:
        run = self._require_run(run_id)
        if kind == "close":
            run.page.close()
            return {"closed": True}
        if kind == "recipient":
            run.page.get_by_test_id("recipient").select_option("synthetic-recipient-b")
        elif kind == "draft":
            self._composer(run).fill(_TAMPER_TEXT)
        elif kind == "reload":
            run.page.reload(wait_until="domcontentloaded")
        return self._snapshot(run_id)

    def _screenshot(self, run_id: str) -> bytes:
        self._snapshot(run_id)
        run = self._require_run(run_id)
        image = run.page.screenshot(type="png", full_page=True, animations="disabled")
        self._require_run(run_id)
        return image

    def _close_run(self, run_id: str) -> None:
        run = self._runs.pop(run_id, None)
        if run is not None:
            run.closing = True
            with suppress(Exception):
                run.context.close()

    def _close_all(self) -> None:
        for run_id in list(self._runs):
            self._close_run(run_id)
        if self._browser is not None:
            with suppress(Exception):
                self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                self._playwright.stop()
            self._playwright = None
