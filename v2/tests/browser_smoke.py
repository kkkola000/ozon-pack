"""Run against an isolated v2 server; uses a temporary owner, no marketplace actions."""

import argparse
import getpass
import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def check_accounts(page, context, url: str, output: Path) -> None:
    """Opt-in write test: isolated database and mocked marketplace only."""
    client_id = "9000" + str(time.time_ns())[-12:]
    key = "browser-synthetic-api-key"
    page.goto(url + "/settings?tab=accounts")
    add = page.locator('form[action="/settings/accounts"]')
    if not add.is_visible():
        page.locator("#add-account>summary").click()
    add.locator('input[name="title"]').fill("Тестовый кабинет Ozon")
    add.locator('input[name="client_id"]').fill(client_id)
    add.locator('input[name="api_key"]').fill(key)
    add.get_by_role("button", name="Сохранить", exact=True).click()
    page.wait_for_url("**notice=saved")
    assert page.get_by_role("status").inner_text() == "Кабинет сохранён"
    card = page.locator(".account-card").filter(has_text=client_id)
    account_id = card.get_attribute("data-account-id")
    assert key not in page.content()
    card.get_by_role("button", name="Проверить доступ", exact=True).click()
    page.wait_for_url("**notice=checked")
    card = page.locator(f'[data-account-id="{account_id}"]')
    assert card.locator(".account-access li").count() == 3
    assert "Чтение доступно" in card.inner_text()
    card.locator(".account-editor>summary").click()
    edit = card.locator(".account-editor form")
    assert edit.locator('input[name="api_key"]').input_value() == ""
    edit.locator('input[name="title"]').fill("Переименованный кабинет")
    edit.get_by_role("button", name="Сохранить", exact=True).click()
    page.wait_for_url("**notice=updated")
    card = page.locator(f'[data-account-id="{account_id}"]')
    assert "Переименованный кабинет" in card.inner_text() and "Чтение доступно" in card.inner_text()
    page.screenshot(path=str(output / "accounts-desktop.png"), full_page=True)
    card.locator(".account-editor>summary").click()
    card.locator('input[name="api_key"]').fill("browser-replacement-key")
    card.locator(".account-editor").get_by_role("button", name="Сохранить", exact=True).click()
    page.wait_for_url("**notice=updated")
    card = page.locator(f'[data-account-id="{account_id}"]')
    assert "Не проверен" in card.inner_text()
    card.get_by_role("button", name="Отключить", exact=True).click()
    page.wait_for_url("**notice=active")
    values = context.request.get(url + "/api/v2/accounts").json()
    assert not next(v for v in values if v["id"] == account_id)["active"]
    assert key not in page.content() and "browser-replacement-key" not in page.content()
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(output / "accounts-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 1440, "height": 1000})


def check_catalog(page, context, url: str, output: Path) -> None:
    """Requires an isolated server with a live worker and mocked Ozon reader."""
    csrf = context.request.get(url + "/api/v2/me").json()["csrf"]
    response = context.request.post(
        url + "/api/v2/accounts",
        headers={"x-csrf-token": csrf},
        data={
            "title": "Каталог Ozon",
            "client_id": "7000" + str(time.time_ns())[-12:],
            "api_key": "catalog-browser-synthetic-key",
        },
    )
    assert response.status == 201
    account = response.json()["id"]
    page.goto(url + "/products?account_id=" + account)
    page.get_by_role("button", name="Обновить товары", exact=True).click()
    page.wait_for_url("**queued=1*")
    badge = page.locator(f'[data-account-id="{account}"] [data-sync-badge]')
    expect(badge).to_have_text("Загружено", timeout=20000)
    refresh = page.locator("#catalog-refresh")
    if refresh.is_visible():
        refresh.click()
    else:
        page.reload()
    expect(page.locator(".catalog-table tr[data-offer-id]")).to_have_count(2)
    page.screenshot(path=str(output / "catalog-desktop.png"), full_page=True)
    page.locator('input[name="q"]').fill("04660291070982")
    page.get_by_role("button", name="Найти", exact=True).click()
    page.wait_for_url("**q=04660291070982*")
    expect(page.locator(".catalog-table tr[data-offer-id]")).to_have_count(1)
    assert "OZN634662543" in page.locator(".catalog-table").inner_text()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/products?account_id=" + account)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(output / "catalog-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})


def check_orders(page, context, url: str, output: Path) -> None:
    csrf = context.request.get(url + "/api/v2/me").json()["csrf"]
    response = context.request.post(
        url + "/api/v2/accounts",
        headers={"x-csrf-token": csrf},
        data={
            "title": "Заказы Ozon",
            "client_id": "6000" + str(time.time_ns())[-12:],
            "api_key": "orders-browser-synthetic-key",
        },
    )
    assert response.status == 201
    account = response.json()["id"]
    response = context.request.post(
        url + "/api/v2/catalog/sync", headers={"x-csrf-token": csrf}, data={"account_id": account}
    )
    assert response.status == 202
    page.goto(url + "/products?account_id=" + account)
    expect(page.locator(f'[data-account-id="{account}"] [data-sync-badge]')).to_have_text(
        "Загружено", timeout=20000
    )
    page.goto(url + "/orders?account_id=" + account)
    page.get_by_role("button", name="Обновить заказы", exact=True).click()
    page.wait_for_url("**queued=1*")
    expect(page.locator(f'[data-account-id="{account}"] [data-sync-badge]')).to_have_text(
        "Загружено", timeout=20000
    )
    refresh = page.locator("#orders-refresh")
    if refresh.is_visible():
        refresh.click()
    else:
        page.reload()
    expect(page.locator(".orders-table tr[data-shipment-id]")).to_have_count(2)
    page.screenshot(path=str(output / "orders-desktop.png"), full_page=True)
    page.locator('input[name="q"]').fill("ii50082908916")
    page.get_by_role("button", name="Найти", exact=True).click()
    page.wait_for_url("**q=ii50082908916*")
    expect(page.locator(".orders-table tr[data-shipment-id]")).to_have_count(1)
    page.get_by_role("link", name="Открыть", exact=True).click()
    expect(page.locator("h1")).to_have_text("59511155-0034-1")
    assert "04660291070982" in page.locator(".order-items").first.inner_text()
    expect(page.locator(".order-item[data-item-id]")).to_have_count(2)
    page.screenshot(path=str(output / "order-detail-desktop.png"), full_page=True)
    page.get_by_role("link", name="59511155-0034-2", exact=True).click()
    expect(page.locator("h1")).to_have_text("59511155-0034-2")
    child_url = page.url
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/orders?account_id=" + account)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(output / "orders-mobile.png"), full_page=True)
    page.goto(child_url)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(output / "order-detail-mobile.png"), full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})


def check(
    url: str,
    login: str,
    password: str,
    chromium: str,
    output: Path,
    include_accounts: bool = False,
    include_catalog: bool = False,
    include_orders: bool = False,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=chromium, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/login")
        page.screenshot(path=str(output / "login-desktop.png"), full_page=True)
        page.locator("input[name=login]").fill(login)
        page.locator("input[name=password]").fill(password)
        page.get_by_role("button", name="Войти", exact=True).click()
        page.wait_for_url("**/pack")
        assert page.locator("h1").inner_text() == "Сборка"
        assert page.locator(".topbar nav>a").first.inner_text() == "Сборка"
        accent = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
        )
        assert accent == "#005bff"
        page.locator(".reports-menu>summary").hover()
        assert page.locator(".reports-menu").evaluate("el => el.open")
        page.locator('.reports-menu a[href="/reports/stickers"]').click()
        page.wait_for_url("**/reports/stickers")
        assert page.locator("h1").inner_text() == "Стикеры"
        page.locator(".account-menu>summary").hover()
        page.locator(".account-menu>summary").click()
        assert page.locator(".account-menu").evaluate("el => el.open")
        page.locator(".account-menu a").click()
        page.wait_for_url("**/settings")
        assert page.locator(".tabs .tab").count() == 4
        assert len(context.request.get(url + "/api/v2/modules").json()) == 11
        page.screenshot(path=str(output / "settings-desktop.png"), full_page=True)
        if include_accounts:
            check_accounts(page, context, url, output)
        if include_catalog:
            check_catalog(page, context, url, output)
        if include_orders:
            check_orders(page, context, url, output)
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(url + "/pack")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "assembly-mobile.png"), full_page=True)
        page.locator(".account-menu>summary").click()
        page.get_by_role("button", name="Выйти", exact=True).click()
        page.wait_for_url("**/login")

        # A real touch context must retain native summary/tap behavior.
        mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        touch = mobile.new_page()
        touch.on("pageerror", lambda error: errors.append(str(error)))
        touch.goto(url + "/login")
        touch.locator("input[name=login]").fill(login)
        touch.locator("input[name=password]").fill(password)
        touch.get_by_role("button", name="Войти", exact=True).tap()
        touch.wait_for_url("**/pack")
        touch.locator(".account-menu>summary").tap()
        assert touch.locator(".account-menu").evaluate("el => el.open")
        touch.get_by_role("button", name="Выйти", exact=True).tap()
        touch.wait_for_url("**/login")
        assert not errors, errors
        browser.close()
    (output / "browser-check.json").write_text(
        json.dumps({"result": "passed", "javascript_errors": errors}, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--login", required=True)
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--output", type=Path, default=Path("test-results"))
    parser.add_argument(
        "--check-accounts",
        action="store_true",
        help="Create a synthetic account on an isolated server with mocked Ozon",
    )
    parser.add_argument(
        "--check-catalog",
        action="store_true",
        help="Test catalog load using mocked Ozon and an isolated worker",
    )
    parser.add_argument(
        "--check-orders", action="store_true", help="Test orders using an isolated worker and mocked Ozon"
    )
    args = parser.parse_args()
    password = os.environ.get("PACK_V2_BROWSER_PASSWORD") or getpass.getpass("Пароль тестового владельца: ")
    check(
        args.url.rstrip("/"),
        args.login,
        password,
        args.chromium,
        args.output,
        args.check_accounts,
        args.check_catalog,
        args.check_orders,
    )
    print("Browser checks passed: desktop hover/click, mobile touch, logout; no JavaScript errors")
