"""Actual browser UI -> HTTP controller -> second managed Chromium -> observed receipt.

These are unmocked local fixture interactions, NOT speech/device/provider validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from .test_app import live_app

pytestmark = pytest.mark.browser


@pytest.fixture
def ui(tmp_path):
    with live_app(tmp_path / "data") as (_, app, origin), sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, chromium_sandbox=True)
        context = browser.new_context(service_workers="block", viewport={"width": 1360, "height": 1020})
        outbound = []

        def route_request(route):
            url = urlsplit(route.request.url)
            if f"{url.scheme}://{url.netloc}" == origin:
                route.continue_()
            else:
                outbound.append(route.request.url)
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin)
        expect(page.locator("#fixture option")).to_have_count(3)
        yield page, app, origin
        assert outbound == [], "UI attempted an external request"
        assert errors == [], "UI JavaScript errors occurred"
        context.close()
        browser.close()


def begin(page, *, fixture="unicode", surface="contenteditable"):
    page.locator("#fixture").select_option(fixture)
    page.locator("#surface").select_option(surface)
    page.locator("#start").click()
    expect(page.locator("#status")).to_have_text("pending", timeout=15000)
    expect(page.locator("#approve")).to_be_enabled()


def test_complete_ui_approval_workflow_and_mobile_layout(ui):
    page, _, _ = ui
    begin(page)
    args = json.loads(page.locator("#arguments").inner_text())
    assert "東京" in args["text"]
    expect(page.locator("#preview")).to_be_visible()
    page.locator("#approve").click()
    expect(page.locator("#status")).to_have_text("completed", timeout=15000)
    receipt = json.loads(page.locator("#receipt").inner_text())
    assert receipt["verified"] is True
    assert receipt["save_count"] == 1
    assert receipt["content"] == args["text"]
    expect(page.locator("#approve")).to_be_disabled()
    expect(page.locator("#events")).to_contain_text("action_completed")
    artifacts = Path(__file__).resolve().parents[1] / "test-artifacts"
    artifacts.mkdir(exist_ok=True)
    page.screenshot(path=str(artifacts / "ui-approved.png"), full_page=False)
    page.locator(".preview-panel").screenshot(path=str(artifacts / "ui-browser-preview.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(artifacts / "ui-mobile.png"), full_page=False)


def test_ui_denial_then_explicit_memory_update(ui):
    page, _, _ = ui
    begin(page, fixture="kickoff", surface="textarea")
    page.locator("#deny").click()
    expect(page.locator("#status")).to_have_text("denied")
    expect(page.locator("#receipt-wrap")).to_be_hidden()
    page.locator(".memory-editor summary").click()
    page.locator("#fact-value").fill("16:00 UTC — controlled UI fixture")
    page.locator("#remember").click()
    expect(page.locator("#status")).to_have_text("pending")
    expect(page.locator("#facts")).not_to_contain_text("16:00 UTC")
    page.locator("#approve").click()
    expect(page.locator("#status")).to_have_text("completed")
    expect(page.locator("#facts")).to_contain_text("16:00 UTC — controlled UI fixture")
    assert json.loads(page.locator("#receipt").inner_text())["kind"] == "set_fact"


def test_ui_changed_recipient_rejects_stale_approval(ui):
    page, _, _ = ui
    begin(page, surface="iframe")
    page.locator("#faults summary").click()
    page.locator('[data-fault="recipient"]').click()
    expect(page.locator("#approve")).to_be_enabled()
    page.locator("#approve").click()
    expect(page.locator("#status")).to_have_text("stale", timeout=10000)
    expect(page.locator("#run-reason")).to_contain_text("binding changed")
    expect(page.locator("#receipt-wrap")).to_be_hidden()


def test_ui_reload_does_not_approve_or_replay(ui):
    page, _, _ = ui
    begin(page, surface="delayed")
    run_id = page.locator("#run-label").get_attribute("data-run-id")
    page.reload()
    expect(page.locator("#status")).to_have_text("pending")
    assert page.locator("#run-label").get_attribute("data-run-id") == run_id
    expect(page.locator("#receipt-wrap")).to_be_hidden()
    page.locator("#stop").click()
    expect(page.locator("#status")).to_have_text("cancelled")
    expect(page.locator("#approve")).to_be_disabled()
