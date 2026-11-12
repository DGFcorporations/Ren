"""Owned Chromium session for Ren mouse and browser control.

Navigation is explicit. Clicks, typing, and pointer motion wait for confirm
so a connected agent cannot act on a page by surprise.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

MUTATING = {"click", "type", "move", "scroll"}


class BrowserError(ValueError):
    pass


def check_url(url: str) -> str:
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BrowserError("Only http and https URLs are allowed")
    if parsed.username or parsed.password:
        raise BrowserError("URLs with embedded credentials are refused")
    return raw


class BrowserController:
    def __init__(self):
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        self._page = None
        self._headed = False
        self._pending: dict[str, dict] = {}

    def _ensure(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError("Playwright is not installed. Run: python -m playwright install chromium") from exc
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=False)
            self._headed = True
        except Exception:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._headed = False
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = context.new_page()
        return self._page

    def snapshot(self) -> dict:
        with self._lock:
            page = self._ensure()
            try:
                text = page.inner_text("body")
            except Exception:
                text = ""
            return {
                "type": "browser_snapshot",
                "url": page.url,
                "title": page.title(),
                "headed": self._headed,
                "text": (text or "")[:8000],
            }

    def navigate(self, url: str) -> dict:
        safe = check_url(url)
        with self._lock:
            page = self._ensure()
            page.goto(safe, wait_until="domcontentloaded", timeout=20000)
            snap = {
                "type": "browser_navigated",
                "url": page.url,
                "title": page.title(),
                "headed": self._headed,
            }
        return snap

    def propose(self, op: str, args: dict) -> dict:
        if op not in MUTATING:
            raise BrowserError(f"Unsupported browser action '{op}'")
        approval_id = __import__("os").urandom(8).hex()
        summary = {"op": op}
        if args.get("selector"):
            summary["selector"] = str(args["selector"])[:200]
        if args.get("text"):
            summary["text_preview"] = str(args["text"])[:240]
        if args.get("x") is not None:
            summary["x"] = args.get("x")
            summary["y"] = args.get("y")
        self._pending[approval_id] = {"op": op, "args": dict(args), "expires": time.time() + 120}
        return {
            "type": "browser_preview",
            "approval_id": approval_id,
            "requires_confirm": True,
            "committed": False,
            **summary,
        }

    def commit(self, approval_id: str) -> dict:
        item = self._pending.get(approval_id)
        if not item:
            raise BrowserError("Unknown browser approval")
        if item["expires"] < time.time():
            self._pending.pop(approval_id, None)
            raise BrowserError("Browser approval expired")
        op = item["op"]
        args = item["args"]
        with self._lock:
            page = self._ensure()
            selector = args.get("selector") or None
            if op == "move":
                page.mouse.move(float(args.get("x") or 0), float(args.get("y") or 0))
            elif op == "click":
                if selector:
                    page.click(selector, timeout=8000)
                else:
                    page.mouse.click(float(args.get("x") or 0), float(args.get("y") or 0))
            elif op == "type":
                text = str(args.get("text") or "")
                if selector:
                    page.fill(selector, text, timeout=8000)
                else:
                    page.keyboard.type(text)
            elif op == "scroll":
                page.mouse.wheel(0, float(args.get("delta_y") or 400))
            url = page.url
            title = page.title()
        self._pending.pop(approval_id, None)
        return {"type": "browser_done", "op": op, "url": url, "title": title, "committed": True, "headed": self._headed}

    def act(
        self,
        op: str,
        *,
        confirm: bool = False,
        approval_id: str | None = None,
        url: str | None = None,
        **args,
    ) -> dict:
        if approval_id:
            return self.commit(approval_id)
        if op == "snapshot":
            return self.snapshot()
        if op == "navigate":
            return self.navigate(url or args.get("url") or "")
        proposal = self.propose(op, {"url": url, **args})
        if not confirm:
            return proposal
        return self.commit(proposal["approval_id"])

    def close(self) -> None:
        with self._lock:
            try:
                if self._browser is not None:
                    self._browser.close()
            finally:
                if self._playwright is not None:
                    self._playwright.stop()
                self._browser = None
                self._page = None
                self._playwright = None
