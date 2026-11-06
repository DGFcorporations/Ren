"""Contract checks are separate from real local Chromium integration checks.

Run contract checks with ``pytest tests/test_browser.py -m 'not browser'``.
Run real browser checks with ``pytest tests/test_browser.py -m browser -rs``.
A missing sandboxed Chromium explicitly leaves the latter UNRUN/skipped; those
skips are not real browser passes. No website, account, credential, paid call,
package installation, or sandbox override is used by these tests.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from workbench_pilot.browser import BrowserUnavailable, FixtureBrowser, FixtureViolation

_STATIC = Path(__file__).resolve().parents[1] / "src" / "workbench_pilot" / "static"
_FILES = {
    "/fixtures/draftbox": ("draftbox.html", "text/html; charset=utf-8"),
    "/fixtures/assets/draftbox.js": ("draftbox.js", "text/javascript; charset=utf-8"),
    "/fixtures/assets/draftbox.css": ("draftbox.css", "text/css; charset=utf-8"),
    "/fixtures/assets/frame.html": ("frame.html", "text/html; charset=utf-8"),
}


@dataclass
class _FixtureServer:
    origin: str = ""
    hits: list[str] = field(default_factory=list)
    script_suffix: str = ""
    overrides: dict[str, tuple[int, dict[str, str], bytes]] = field(default_factory=dict)


@contextmanager
def _serve_fixture():
    state = _FixtureServer()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.hits.append(self.path)
            path = urlsplit(self.path).path
            if path in state.overrides:
                status, headers, body = state.overrides[path]
            elif path in _FILES:
                filename, content_type = _FILES[path]
                body = (_STATIC / filename).read_bytes()
                if filename == "draftbox.js":
                    body += state.script_suffix.encode("utf-8")
                status, headers = 200, {"Content-Type": content_type}
            else:
                status, headers, body = 404, {"Content-Type": "text/plain"}, b"Not a fixture"
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.origin = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, name="synthetic-fixture-http", daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def fixture_server():
    with _serve_fixture() as server:
        yield server


@pytest.fixture(scope="module")
def sandboxed_chromium_available():
    with _serve_fixture() as server:
        worker = FixtureBrowser(server.origin)
        try:
            worker.open("browser-availability-probe")
        except BrowserUnavailable as exc:
            pytest.skip(
                "UNRUN real browser integration: sandboxed Chromium is unavailable; "
                "no fallback or security weakening. " + str(exc).splitlines()[0]
            )
        finally:
            worker.close()


@pytest.fixture
def browser(sandboxed_chromium_available, fixture_server):
    worker = FixtureBrowser(fixture_server.origin)
    try:
        yield worker
    finally:
        worker.close()


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:43123",
        "http://localhost:43123",
        "http://[::1]:43123",
        "http://127.12.34.56:65535",
        "http://127.0.0.1:1",
        "http://127.0.0.1:80",
    ],
)
def test_canonical_loopback_origin_is_lazy(origin):
    worker = FixtureBrowser(origin)
    try:
        assert worker.origin == origin
        assert worker._browser is None
        assert worker._playwright is None
    finally:
        worker.close()


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        42,
        "https://127.0.0.1:8080",
        "http://example.invalid:8080",
        "http://localhost.example.invalid:8080",
        "http://127.0.0.1.nip.invalid:8080",
        "http://0.0.0.0:8080",
        "http://192.168.1.1:8080",
        "http://[::]:8080",
        "http://[::ffff:127.0.0.1]:8080",
        "http://[0:0:0:0:0:0:0:1]:8080",
        "http://2130706433:8080",
        "http://0x7f000001:8080",
        "http://127.1:8080",
        "http://127.000.000.001:8080",
        "http://127.0.0.1.:8080",
        "http://LOCALHOST:8080",
        "http://localhost.:8080",
        "http://user:pass@127.0.0.1:8080",
        "http://127.0.0.1:8080@elsewhere.invalid",
        "http://127.0.0.1:8080\\@elsewhere.invalid",
        "http://127.0.0.1:8080/",
        "http://127.0.0.1:8080/fixtures",
        "http://127.0.0.1:8080?x=1",
        "http://127.0.0.1:8080#",
        "http://127.0.0.1:8080#anything",
        " http://127.0.0.1:8080",
        "http://127.0.0.1:8080\n",
        "http://127.0.0.1:\t8080",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:08080",
        "http://127.0.0.1:+8080",
        "http://127.0.0.1:８０８０",
        "http://127.0.0.1",
        "http://127.0.0.1:abc",
        "file:///fixtures/draftbox",
        "//127.0.0.1:8080",
    ],
)
def test_ambiguous_non_loopback_or_non_origin_arguments_are_rejected(origin):
    with pytest.raises(FixtureViolation):
        FixtureBrowser(origin)


def test_url_policy_has_no_origin_path_query_or_port_escape():
    worker = FixtureBrowser("http://127.0.0.1:8080")
    query = "run_id=synthetic-run&surface=textarea"
    base = worker.origin
    try:
        assert worker._url_allowed(f"{base}/fixtures/draftbox?{query}", "synthetic-run", "textarea")
        assert worker._url_allowed(f"{base}/fixtures/assets/draftbox.js", "synthetic-run", "textarea")
        forbidden = [
            f"http://127.0.0.1:8081/fixtures/draftbox?{query}",
            f"http://localhost:8080/fixtures/draftbox?{query}",
            f"http://127.0.0.1/fixtures/draftbox?{query}",
            f"http://127.0.0.1:08080/fixtures/draftbox?{query}",
            f"http://127.0.0.1:8080@elsewhere.invalid/fixtures/draftbox?{query}",
            f"{base}/api/execute",
            f"{base}/fixtures/../api/execute",
            f"{base}/fixtures/%2e%2e/api/execute",
            f"{base}/fixtures/%2f..%2fapi/execute",
            f"{base}/fixtures//assets/draftbox.js",
            f"{base}/fixtures/assets/../../api/execute",
            f"{base}/fixtures/assets/not-owned.js",
            f"{base}/fixtures/assets/frame.html?redirect=/api/execute",
            f"{base}/fixtures/assets/draftbox.js?",
            f"{base}/fixtures/assets/draftbox.js#",
            f"{base}/fixtures/assets/draftbox.js\\..\\api",
            f"{base}/fixtures/draftbox?{query}&surface=iframe",
            f"{base}/fixtures/draftbox?{query}&run_id=other",
            f"{base}/fixtures/draftbox?run_id=other&surface=textarea",
            f"{base}/fixtures/draftbox?{query}#fragment",
            f"{base}/fixtures/draftbox?{query}\n",
            "data:text/html,synthetic",
            "about:blank",
            "file:///fixtures/assets/frame.html",
            "javascript:void(0)",
            "http://elsewhere.invalid:8080/fixtures/assets/draftbox.js",
        ]
        for url in forbidden:
            assert not worker._url_allowed(url, "synthetic-run", "textarea"), url
        assert not worker._url_allowed(
            f"{base}/fixtures/assets/frame.html", "synthetic-run", "textarea", main=True
        )
    finally:
        worker.close()


def test_default_port_normalization_does_not_admit_another_origin():
    worker = FixtureBrowser("http://127.0.0.1:80")
    try:
        assert worker._url_allowed("http://127.0.0.1/fixtures/assets/draftbox.css", "r", "textarea")
        assert not worker._url_allowed("http://localhost/fixtures/assets/draftbox.css", "r", "textarea")
        assert not worker._url_allowed("http://127.0.0.1:81/fixtures/assets/draftbox.css", "r", "textarea")
    finally:
        worker.close()


def test_arguments_unknown_runs_and_close_do_not_start_a_browser():
    worker = FixtureBrowser("http://127.0.0.1:8080")
    try:
        for run_id in (None, "", "r\n", "r" * 129):
            with pytest.raises(FixtureViolation):
                worker.open(run_id)
        for surface in (None, [], "javascript", "textarea; document.cookie"):
            with pytest.raises(FixtureViolation):
                worker.open("r", surface)
        for text in (None, ["text"], "x" * 8001):
            with pytest.raises(FixtureViolation):
                worker.execute("r", text)
        for kind in (None, [], "navigate", "evaluate", "http://elsewhere.invalid"):
            with pytest.raises(FixtureViolation):
                worker.tamper("r", kind)
        with pytest.raises(FixtureViolation, match="Unknown"):
            worker.snapshot("r")
        with pytest.raises(FixtureViolation, match="Unknown"):
            worker.screenshot("r")
        worker.close_run("unknown")  # Idempotent cleanup does not launch anything.
        assert worker._browser is None
        assert worker._playwright is None
    finally:
        worker.close()
    worker.close()
    with pytest.raises(FixtureViolation, match="closed"):
        worker.open("r")


def test_launch_failure_contract_keeps_sandbox_and_has_no_success_fallback(monkeypatch):
    """Mocked failure-path CONTRACT test; not evidence of a real browser run."""
    from playwright import sync_api

    launch_calls = []
    worker_threads = []

    class UnavailableRuntime:
        chromium = None
        stopped = False

        def start(self):
            self.chromium = self
            return self

        def launch(self, **kwargs):
            launch_calls.append(kwargs)
            worker_threads.append(threading.get_ident())
            raise RuntimeError("Deliberately unavailable test Chromium")

        def stop(self):
            self.stopped = True

    runtime = UnavailableRuntime()
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: runtime)
    worker = FixtureBrowser("http://127.0.0.1:8080")
    try:
        with pytest.raises(BrowserUnavailable, match="no insecure or simulated fallback"):
            worker.open("cannot-launch")
        assert launch_calls == [{"headless": True, "chromium_sandbox": True}]
        assert runtime.stopped
        assert worker_threads[0] != threading.get_ident()
        assert worker._browser is None
    finally:
        worker.close()


@pytest.mark.browser
class TestRealLocalBrowser:
    @pytest.mark.parametrize("surface", ["textarea", "contenteditable", "iframe", "delayed"])
    def test_all_four_real_surfaces_and_verified_receipts(self, browser, fixture_server, surface):
        run_id = f"real-{surface}"
        before = browser.open(run_id, surface)
        assert set(before) == {
            "origin",
            "tab_id",
            "account",
            "recipient",
            "page_epoch",
            "surface",
            "before_text",
            "save_count",
        }
        assert before == browser.snapshot(run_id)
        assert before["origin"] == fixture_server.origin
        assert before["tab_id"] == run_id
        assert before["account"] == "synthetic-account-a"
        assert before["recipient"] == "synthetic-recipient-a"
        assert before["surface"] == surface
        assert before["before_text"] == ""
        assert before["save_count"] == 0
        text = "<b>Literal synthetic text</b> & café ☕\nSecond line, not HTML."
        receipt = browser.execute(run_id, text)
        assert receipt == {
            "content": text,
            "recipient": before["recipient"],
            "account": before["account"],
            "save_count": 1,
            "verified": True,
            "origin": fixture_server.origin,
            "tab_id": run_id,
        }
        assert browser.snapshot(run_id) == {**before, "before_text": text, "save_count": 1}
        assert browser.execute(run_id, "Second approved synthetic draft")["save_count"] == 2

    def test_exact_context_tampers_reload_and_close(self, browser):
        before = browser.open("tamper")
        draft = browser.tamper("tamper", "draft")
        assert draft["before_text"] == "Synthetic preexisting draft (controlled fixture tamper)."
        assert draft == {**before, "before_text": draft["before_text"]}
        recipient = browser.tamper("tamper", "recipient")
        assert recipient == {**draft, "recipient": "synthetic-recipient-b"}
        receipt = browser.execute("tamper", "Replacement synthetic draft")
        assert receipt["recipient"] == "synthetic-recipient-b"
        reloaded = browser.tamper("tamper", "reload")
        assert reloaded["page_epoch"] != before["page_epoch"]
        assert reloaded == {**before, "page_epoch": reloaded["page_epoch"]}
        assert browser.tamper("tamper", "close") == {"closed": True}
        with pytest.raises(FixtureViolation, match="closed"):
            browser.snapshot("tamper")
        browser.close_run("tamper")
        browser.close_run("tamper")

    def test_real_screenshot_is_png_bytes(self, browser):
        browser.open("screenshot")
        image = browser.screenshot("screenshot")
        assert isinstance(image, bytes)
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(image) > 1000

    def test_anonymous_contexts_do_not_share_fixture_cookies_or_storage(self, browser, fixture_server):
        fixture_server.script_suffix = """
        (() => {
          const previous = document.cookie || localStorage.getItem('synthetic-marker');
          if (previous) document.querySelector('[data-testid="composer"]').value = 'CONTEXT LEAK';
          document.cookie = 'synthetic-marker=only-this-context; SameSite=Strict';
          localStorage.setItem('synthetic-marker', 'only-this-context');
        })();
        """
        first = browser.open("context-one")
        browser.tamper("context-one", "draft")
        second = browser.open("context-two")
        assert first["before_text"] == second["before_text"] == ""
        assert first["page_epoch"] != second["page_epoch"]
        assert browser.snapshot("context-one")["before_text"] != ""

    def test_run_cap_repeated_open_and_cleanup(self, browser):
        snapshots = [browser.open(f"cap-{index}") for index in range(5)]
        assert browser.open("cap-0") == snapshots[0]
        with pytest.raises(FixtureViolation, match="surface"):
            browser.open("cap-0", "iframe")
        with pytest.raises(FixtureViolation, match="five"):
            browser.open("cap-5")
        browser.close_run("cap-0")
        assert browser.open("cap-5")["save_count"] == 0
        with pytest.raises(FixtureViolation, match="Unknown"):
            browser.snapshot("cap-0")

    def test_real_playwright_objects_remain_on_one_worker_thread(self, browser):
        run_ids = [f"thread-{index}" for index in range(3)]
        with ThreadPoolExecutor(max_workers=3) as callers:
            opened = list(callers.map(browser.open, run_ids))
            assert {snapshot["tab_id"] for snapshot in opened} == set(run_ids)
            receipts = list(
                callers.map(lambda run_id: browser.execute(run_id, f"Draft for {run_id}"), run_ids)
            )
            assert all(receipt["verified"] and receipt["save_count"] == 1 for receipt in receipts)
            snapshots = list(callers.map(browser.snapshot, run_ids))
            assert all(snapshot["save_count"] == 1 for snapshot in snapshots)

    def test_text_limit_and_empty_draft_are_real_operations(self, browser):
        browser.open("text-limit")
        with pytest.raises(FixtureViolation):
            browser.execute("text-limit", "x" * 8001)
        assert browser.snapshot("text-limit")["save_count"] == 0
        assert browser.execute("text-limit", "x" * 8000)["content"] == "x" * 8000
        assert browser.execute("text-limit", "")["content"] == ""

    def test_run_id_is_encoded_as_data_not_as_a_url(self, browser):
        run_id = "synthetic &surface=iframe/#? quoted '☕'"
        snapshot = browser.open(run_id)
        assert snapshot["tab_id"] == run_id
        assert snapshot["surface"] == "textarea"
        assert browser.execute(run_id, "A synthetic draft")["verified"] is True

    def test_receipt_mismatch_after_actual_save_propagates(self, browser, fixture_server):
        fixture_server.script_suffix = """
        document.querySelector('[data-testid="save-draft"]').addEventListener('click', () => {
          document.querySelector('[data-testid="saved-content"]').textContent = 'Controlled corrupt receipt';
        });
        """
        browser.open("receipt-mismatch")
        with pytest.raises(FixtureViolation, match="did not verify"):
            browser.execute("receipt-mismatch", "Requested synthetic draft")
        # The write happened; an exception must not be reported as a verified
        # success or silently retried by the worker/controller.
        assert browser.snapshot("receipt-mismatch")["save_count"] == 1

    @pytest.mark.parametrize("redirect_kind", ["other-port", "app-api", "encoded-path", "allowed-asset"])
    def test_redirect_is_not_followed_even_when_target_looks_local(
        self, browser, fixture_server, redirect_kind
    ):
        with _serve_fixture() as witness:
            target = {
                "other-port": witness.origin + "/witness",
                "app-api": fixture_server.origin + "/api/control",
                "encoded-path": fixture_server.origin + "/fixtures/%2e%2e/api/control",
                "allowed-asset": fixture_server.origin + "/fixtures/assets/frame.html",
            }[redirect_kind]
            fixture_server.overrides["/fixtures/draftbox"] = (302, {"Location": target}, b"Redirect")
            with pytest.raises(FixtureViolation, match="redirect"):
                browser.open("redirect")
            assert witness.hits == []
            assert len(fixture_server.hits) == 1
            with pytest.raises(FixtureViolation, match="Unknown"):
                browser.snapshot("redirect")

    def test_iframe_redirect_is_not_followed(self, browser, fixture_server):
        with _serve_fixture() as witness:
            fixture_server.overrides["/fixtures/assets/frame.html"] = (
                307,
                {"Location": witness.origin + "/witness"},
                b"Redirect",
            )
            with pytest.raises(FixtureViolation, match="redirect"):
                browser.open("frame-redirect", "iframe")
            assert witness.hits == []

    def test_main_navigation_cannot_reach_app_control_endpoints(self, browser, fixture_server):
        fixture_server.script_suffix = '\nwindow.location.assign("/api/control");\n'
        with pytest.raises(FixtureViolation):
            browser.open("navigation-escape")
        assert not any(urlsplit(hit).path == "/api/control" for hit in fixture_server.hits)

    def test_popup_is_routed_before_its_first_navigation(self, browser, fixture_server):
        fixture_server.script_suffix = '\nwindow.open(window.location.href, "unwanted-popup");\n'
        with pytest.raises(FixtureViolation):
            browser.open("popup")
        # A popup targeting the otherwise allowed URL must still never fetch it.
        main_requests = [hit for hit in fixture_server.hits if urlsplit(hit).path == "/fixtures/draftbox"]
        assert len(main_requests) == 1

    def test_other_origin_fetch_socket_and_image_requests_never_leave_fixture(self, browser, fixture_server):
        with _serve_fixture() as witness:
            fixture_server.script_suffix = f"""
            fetch('{witness.origin}/witness').catch(() => {{}});
            fetch('/api/control').catch(() => {{}});
            const unwantedImage = new Image();
            unwantedImage.src = '{witness.origin}/image';
            try {{ new WebSocket('{witness.origin.replace("http:", "ws:")}/socket'); }} catch (_) {{}}
            """
            with pytest.raises(FixtureViolation, match="WebSockets"):
                browser.open("blocked-network")
            assert witness.hits == []
            assert all(urlsplit(hit).path in _FILES for hit in fixture_server.hits)


@pytest.mark.browser
def test_approved_snapshot_rechecked_on_worker_thread_before_input(browser):
    original = browser.open("bound-action", "textarea")
    browser.tamper("bound-action", "recipient")
    with pytest.raises(FixtureViolation, match="snapshot changed"):
        browser.execute("bound-action", "must not be entered", expected_snapshot=original)
    after = browser.snapshot("bound-action")
    assert after["before_text"] == ""
    assert after["save_count"] == 0


@pytest.mark.browser
@pytest.mark.parametrize("change", ["recipient", "content"])
def test_event_time_mutation_cannot_commit_wrong_target_or_content(browser, fixture_server, change):
    mutation = (
        """document.querySelector('[data-testid="recipient"]').value = "synthetic-recipient-b";"""
        if change == "recipient"
        else """document.querySelector('[data-testid="composer"]').value = "NOT APPROVED";"""
    )
    fixture_server.script_suffix = (
        """document.querySelector('[data-testid="save-draft"]').addEventListener("pointerdown", () => {"""
        + mutation
        + "});"
    )
    original = browser.open("event-time-mutation", "textarea")
    with pytest.raises(FixtureViolation, match="event-time"):
        browser.execute("event-time-mutation", "approved synthetic draft", expected_snapshot=original)
    assert browser.snapshot("event-time-mutation")["save_count"] == 0
