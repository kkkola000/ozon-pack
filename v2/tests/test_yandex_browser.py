"""Actual scanner, cargo editor and original label controls with the existing design."""

import shutil
from uuid import UUID

import pytest
from conftest import login
from playwright.sync_api import expect, sync_playwright
from test_yandex_fulfillment import flow as flow
from test_yandex_fulfillment import opened, until
from yandex_remote import BARCODE, GTD, ORDER, mark


@pytest.fixture
def browser_page(flow, client):
    assembly, data, _, _, printing = flow
    for name, value in {
        "assembly": assembly,
        "connections": assembly.connections,
        "catalog": assembly.orders.catalog,
        "orders": assembly.orders,
        "instance_data": data,
        "documents": assembly.documents,
        "readiness": assembly.readiness,
        "sticker_archive": printing.archive,
        "printing": printing,
    }.items():
        setattr(client.app.state, name, value)
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
        yield page, errors
        browser.close()


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_yandex_scanner_gtd_status_ready_and_archive(flow, owner, browser_page):
    page, errors = browser_page
    session = opened(flow, owner)
    page.goto("http://testserver/pack")
    expect(page.locator("[data-assembly-account]")).to_contain_text("Яндекс Маркет")
    expect(page.get_by_role("checkbox", name="Не указывать")).to_have_count(0)
    expect(page.get_by_role("button", name="Добавить грузоместо", exact=True)).to_be_visible()
    scanner = page.locator("#assembly-code")
    scanner.fill(BARCODE)
    scanner.press("Enter")
    expect(page.locator("[data-assembly-count]")).to_have_text("1 из 1")
    gtd = page.locator("[data-gtd-unit]")
    gtd.fill(GTD)
    gtd.press("Enter")
    expect(page.locator("[data-assembly-message]")).to_have_text("ГТД сохранена")
    scanner.fill(mark())
    scanner.press("Enter")
    expect(page.locator("[data-scanner-state]")).to_have_text("Проверяем данные…")
    session = until(flow, owner, session, lambda s: s["data"]["can_verify"])
    expect(page.locator(".assembly-mark.accepted")).to_have_text("М", timeout=12000)
    expect(page.get_by_text("Проверка успешно пройдена.", exact=True)).to_be_visible()
    expect(page.locator(".assembly-gtd .access-ok")).to_have_text("ГТД принята Яндекс Маркетом")
    page.get_by_role("button", name="Завершить сборку", exact=True).click()
    expect(page.locator("[data-assembly-stage]")).to_have_text("Яндекс Маркет обрабатывает сборку")
    session = until(flow, owner, session, lambda s: s["status"] == "ready")
    expect(page.locator("[data-assembly-stage]")).to_have_text("Готов к отгрузке", timeout=12000)
    assert not errors
    page.screenshot(path="/tmp/ozon-pack-v2-yandex-fulfillment.png", full_page=True)
    page.goto("http://testserver/reports/stickers")
    expect(page.get_by_role("button", name="Честный знак")).to_be_visible()
    expect(page.get_by_text(ORDER, exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Открыть PDF")).to_be_visible()
    assert not errors


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_add_box_save_and_single_product_scan(flow, owner, browser_page):
    page, errors = browser_page
    flow[2].unmarked()
    session = opened(flow, owner)
    page.goto("http://testserver/pack")
    page.get_by_role("button", name="Добавить грузоместо", exact=True).click()
    expect(page.locator(".assembly-layout-group")).to_have_count(2)
    expect(page.get_by_role("button", name="Сохранить", exact=True)).to_be_visible()
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("[data-scanner-state]")).to_have_text("Проверяем данные…")
    session = until(
        flow,
        owner,
        session,
        lambda s: s["data"]["can_verify"] and any(label["status"] == "ready" for label in s["labels"]),
    )
    expect(page.get_by_role("button", name="Добавить грузоместо", exact=True)).to_be_visible(timeout=12000)
    scanner = page.locator("#assembly-code")
    scanner.fill(BARCODE)
    scanner.press("Enter")
    expect(page.locator("[data-assembly-count]")).to_have_text("1 из 1")
    assert len(flow[4].plan(owner, UUID(session["id"]))["jobs"]) == 2
    assert not errors
    page.screenshot(path="/tmp/ozon-pack-v2-yandex-cargo.png", full_page=True)
