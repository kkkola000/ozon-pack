"""Real browser acquisition/download flows; no live marketplace calls."""

import shutil
from pathlib import Path
from uuid import UUID

import pytest
from conftest import login
from instance_remote import GTD, mark
from playwright.sync_api import expect, sync_playwright
from test_documents import finish_labels, ready_open
from test_documents import labels as labels
from test_instance_data import KONVERT, finish_data
from test_instance_data import pipeline as pipeline
from test_readiness import all_scans, run_ready
from test_readiness import ready_pipeline as ready_pipeline


@pytest.fixture
def browser_page(client, ready_pipeline, labels):
    assembly = ready_pipeline[0]
    client.app.state.assembly = assembly
    client.app.state.documents = assembly.documents
    client.app.state.connections = assembly.connections
    login(client)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"), args=["--no-sandbox"])
        context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1000})
        page = context.new_page()

        def route_request(route):
            req = route.request
            response = client.request(req.method, req.url, headers=req.headers, content=req.post_data_buffer)
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

        page.route("http://testserver/**", route_request)
        page.route("https://**", lambda route: route.abort())
        yield page
        browser.close()


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
@pytest.mark.parametrize("storage_disabled", [False, True])
def test_automatic_download_once_does_not_interrupt_chz_gtd_or_final_scan(
    labels, ready_pipeline, owner, db, browser_page, storage_disabled
):
    page = browser_page
    if storage_disabled:
        page.add_init_script(
            "Storage.prototype.setItem = function() { throw new DOMException('Storage unavailable', 'QuotaExceededError'); };"
        )
    errors, downloads = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("download", lambda download: downloads.append(download))
    view = finish_labels(ready_pipeline, owner, ready_open(ready_pipeline, owner, db))
    with page.expect_download() as result:
        page.goto("http://testserver/pack")
    file = result.value
    assert Path(file.path()).read_bytes() == labels.file
    assert view["posting_number"] in file.suggested_filename
    expect(page.get_by_role("link", name="Открыть PDF", exact=True)).to_be_visible()
    if not storage_disabled:
        page.reload()
        expect(page.get_by_role("link", name="Открыть PDF", exact=True)).to_be_visible()
    assert len(downloads) == 1
    scanner = page.locator("#assembly-code")
    scanner.fill(mark())
    scanner.press("Enter")
    expect(page.locator("[data-assembly-message]")).to_have_text(
        "Честный знак сохранён. Ожидаем подтверждения Ozon"
    )
    gtd = page.locator("[data-gtd-unit]")
    gtd.fill(GTD)
    gtd.press("Enter")
    expect(page.locator("[data-scanner-state]")).to_have_text("Проверяем данные…")
    finish_data(ready_pipeline, owner, view)
    expect(page.locator(".assembly-mark.accepted")).to_have_count(1, timeout=12000)
    for count in (2, 3):
        scanner.fill(KONVERT)
        scanner.press("Enter")
        expect(page.locator("[data-assembly-count]")).to_have_text(f"{count} из 3")
    scanner.fill("ii50082908916")
    scanner.press("Enter")
    expect(page.locator("[data-assembly-stage]")).to_have_text("Ozon обрабатывает сборку")
    run_ready(ready_pipeline)
    expect(page.locator("[data-assembly-stage]")).to_have_text("Готов к отгрузке", timeout=12000)
    with page.expect_download() as manual:
        page.get_by_role("button", name="Скачать стикер", exact=True).click()
    assert Path(manual.value.path()).read_bytes() == labels.file
    assert len(downloads) == 2 and not errors
    assert len(labels.tasks) == 1 and not ready_pipeline[2].ship_calls
    assert ready_pipeline[0].session(owner, UUID(view["id"]))["status"] == "ready"
    stored = page.evaluate("JSON.stringify(sessionStorage)")
    assert mark() not in stored and GTD not in stored and "%PDF" not in stored
    page.screenshot(path="/tmp/ozon-pack-v2-label-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/ozon-pack-v2-label-mobile.png", full_page=True)


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_deferred_label_appears_and_downloads_after_confirmed_readiness(
    labels, ready_pipeline, owner, browser_page
):
    page = browser_page
    view = all_scans(ready_pipeline, owner)
    page.goto("http://testserver/pack")
    expect(page.get_by_role("button", name="Скачать стикер", exact=True)).to_be_disabled()
    expect(page.locator("[data-label-status]")).to_have_text(
        "Стикер будет получен после подтверждения готовности Ozon"
    )
    page.get_by_role("button", name="Завершить сборку", exact=True).click()
    expect(page.locator("[data-assembly-stage]")).to_have_text("Ozon обрабатывает сборку")
    run_ready(ready_pipeline)
    run_ready(ready_pipeline)
    finish_labels(ready_pipeline, owner, view)
    with page.expect_download(timeout=12000) as result:
        expect(page.locator("[data-assembly-stage]")).to_have_text("Готов к отгрузке", timeout=12000)
    assert Path(result.value.path()).read_bytes() == labels.file
    expect(page.get_by_role("link", name="Открыть PDF", exact=True)).to_be_visible()
    assert len(ready_pipeline[2].ship_calls) == 1
