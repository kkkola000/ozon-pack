"""Actual assembly controls against isolated PostgreSQL and contract-shaped Ozon."""

import shutil
from uuid import UUID

import pytest
from conftest import login
from playwright.sync_api import expect, sync_playwright
from test_instance_data import KONVERT, MUFT, finish_data, open_window, state
from test_instance_data import pipeline as pipeline
from test_instance_parity import gtd_only
from test_readiness import ready_pipeline as ready_pipeline
from test_readiness import run_ready


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_omission_checkbox_and_final_label_scan_keep_working(ready_pipeline, owner, client):
    assembly, _, remote, _ = ready_pipeline
    gtd_only(remote)
    view = open_window(ready_pipeline, owner, code=MUFT)
    client.app.state.assembly = assembly
    client.app.state.connections = assembly.connections
    login(client)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"), args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1360, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))

        def request(route):
            req = route.request
            response = client.request(req.method, req.url, headers=req.headers, content=req.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

        page.route("http://testserver/**", request)
        page.route("https://**", lambda route: route.abort())
        page.goto("http://testserver/pack")
        checkbox = page.get_by_role("checkbox", name="Не указывать", exact=True)
        field = page.locator("[data-gtd-unit]")
        checkbox.check()
        expect(checkbox).to_be_checked()
        expect(field).to_be_disabled()
        assert not errors
        page.wait_for_function("!document.querySelector('[data-network-retry]:not([hidden])')")
        expect(page.locator("[data-scanner-state]")).to_have_text("Проверяем данные…")
        view = finish_data(ready_pipeline, owner, view)
        expect(page.locator(".assembly-gtd .access-ok")).to_have_text("ГТД принята Ozon", timeout=12000)
        checkbox.uncheck()
        expect(field).to_be_enabled()
        scanner = page.locator("#assembly-code")
        scanner.fill(KONVERT)
        scanner.press("Enter")
        expect(page.locator("[data-assembly-message]")).to_have_text(
            "Укажите ГТД или выберите «Не указывать»"
        )
        assert state(ready_pipeline, owner, view)["scanned"] == 1
        # Reverting an unsaved edit to the stored choice must release the scanner.
        checkbox.check()
        for expected in (2, 3):
            scanner.fill(KONVERT)
            scanner.press("Enter")
            expect(page.locator("[data-assembly-count]")).to_have_text(f"{expected} из 3")
        scanner.fill("ii50082908916")
        scanner.press("Enter")
        expect(page.locator("[data-assembly-stage]")).to_have_text("Ozon обрабатывает сборку")
        run_ready(ready_pipeline)
        run_ready(ready_pipeline)
        expect(page.locator("[data-assembly-stage]")).to_have_text("Готов к отгрузке", timeout=12000)
        assert assembly.session(owner, UUID(view["id"]))["status"] == "ready"
        assert not errors
        assert len(remote.ship_calls) == 1
        assert "value" not in page.evaluate("JSON.stringify(sessionStorage)")
        page.screenshot(path="/tmp/ozon-pack-v2-gtd-browser.png", full_page=True)
        browser.close()
