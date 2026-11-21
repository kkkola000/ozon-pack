"""Run against an isolated v2 server; uses a temporary owner, no marketplace actions."""

import argparse
import getpass
import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


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


def check(
    url: str, login: str, password: str, chromium: str, output: Path, include_accounts: bool = False
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
    args = parser.parse_args()
    password = os.environ.get("PACK_V2_BROWSER_PASSWORD") or getpass.getpass("Пароль тестового владельца: ")
    check(args.url.rstrip("/"), args.login, password, args.chromium, args.output, args.check_accounts)
    print("Browser checks passed: desktop hover/click, mobile touch, logout; no JavaScript errors")
