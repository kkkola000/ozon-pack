"""Browser coverage for archive paging, private scan search and guarded admin UI."""

import shutil

import pytest
from conftest import PASSWORD, login
from instance_remote import GTIN, mark
from playwright.sync_api import expect, sync_playwright
from test_documents import labels as labels
from test_instance_data import pipeline as pipeline
from test_readiness import ready_pipeline as ready_pipeline
from test_sticker_archive import archive as archive
from test_sticker_archive import make_rows, saved_mark


@pytest.fixture
def archive_page(archive, ready_pipeline, client):
    import socket
    import threading
    import time

    import uvicorn

    from pack_v2.web.deps import COOKIE

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    while port == 50002:  # Reserved Cloud service port.
        sock.close()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(client.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=shutil.which("chromium"), args=["--no-sandbox"]
            )
            context = browser.new_context(
                base_url=origin, viewport={"width": 1440, "height": 1000}, accept_downloads=True
            )
            context.add_cookies([{"name": COOKIE, "value": client.cookies.get(COOKIE), "url": origin}])
            page = context.new_page()
            yield page
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_more_appends_and_date_search_uses_original_style(archive, ready_pipeline, owner, db, archive_page):
    make_rows(archive, ready_pipeline, owner, db)
    page = archive_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("/reports/stickers")
    expect(page.locator("[data-sticker-row]")).to_have_count(30)
    expect(page.locator("input[name=since]")).to_have_value("")
    expect(page.locator("input[name=until]")).to_have_value("")
    page.get_by_role("link", name="Ещё", exact=True).click()
    expect(page.locator("[data-sticker-row]")).to_have_count(60)
    page.get_by_role("link", name="Ещё", exact=True).click()
    expect(page.locator("[data-sticker-row]")).to_have_count(65)
    expect(page.get_by_role("link", name="Ещё", exact=True)).to_have_count(0)
    page.locator("input[name=q]").fill("ii-archive-0001")
    page.get_by_role("button", name="Найти", exact=True).click()
    expect(page.locator("[data-sticker-row]")).to_have_count(1)
    expect(page.locator("[data-sticker-row]")).to_contain_text("archive-0001")
    assert "ii-archive" not in page.url and "?search=" in page.url
    assert not errors
    page.screenshot(path="/tmp/ozon-pack-v2-archive-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/ozon-pack-v2-archive-mobile.png", full_page=True)


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_marking_dialog_unbind_and_full_scan_search(archive, ready_pipeline, owner, db, archive_page):
    saved_mark(ready_pipeline, owner, db)
    page = archive_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto("/reports/stickers")
    page.get_by_role("button", name="Честный знак", exact=False).click()
    dialog = page.locator("[data-marking-dialog]")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(GTIN)
    expect(dialog).to_contain_text("Принят")
    expect(dialog).to_contain_text("Привязан")
    page.get_by_role("button", name="Отвязать", exact=True).click()
    expect(dialog).to_contain_text("Отвязан")
    expect(dialog.get_by_role("button", name="Отвязать", exact=True)).to_have_count(0)
    dialog.get_by_text("Полный код", exact=True).click()
    expect(dialog.locator("code")).to_contain_text("<GS>")
    page.screenshot(path="/tmp/ozon-pack-v2-archive-marking.png", full_page=True)
    page.get_by_role("button", name="Закрыть", exact=True).click()
    page.locator("input[name=q]").fill(mark())
    page.get_by_role("button", name="Найти", exact=True).click()
    expect(page.locator("[data-sticker-row]")).to_have_count(1)
    expect(page.locator("[data-marking-bound]")).to_have_text("Коды отвязаны")
    assert GTIN not in page.url and "91ABCD" not in page.url and "%1D" not in page.url
    assert not errors


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_packer_can_view_codes_and_download_but_cannot_unbind(
    archive, ready_pipeline, owner, db, client, identity, archive_page
):
    saved_mark(ready_pipeline, owner, db)
    identity.create_user(owner, "archive-packer", PASSWORD, "packer", frozenset({"stickers"}))
    csrf = client.get("/api/v2/me").json()["csrf"]
    client.post("/logout", data={"csrf": csrf})
    login(client, "archive-packer")
    page = archive_page
    from pack_v2.web.deps import COOKIE

    cookie = next(value for value in page.context.cookies() if value["name"] == COOKIE)
    cookie["value"] = client.cookies.get(COOKIE)
    page.context.add_cookies([cookie])
    page.goto("/reports/stickers")
    with page.expect_download() as result:
        page.get_by_role("link", name="Скачать", exact=True).click()
    assert result.value.suggested_filename.endswith(".pdf")
    page.get_by_role("button", name="Честный знак", exact=False).click()
    expect(page.locator("[data-marking-dialog]")).to_contain_text("Принят")
    expect(page.get_by_role("button", name="Отвязать", exact=True)).to_have_count(0)
