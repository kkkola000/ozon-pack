"""Chromium with real application HTTP and explicit local printer doubles."""

import base64
import io
import shutil

import pytest
from playwright.sync_api import expect
from pypdf import PdfReader
from sqlalchemy import select
from test_documents import labels as labels
from test_instance_data import pipeline as pipeline
from test_printing import configure, label_job
from test_printing import printing as printing
from test_readiness import ready_pipeline as ready_pipeline
from test_sticker_archive import archive as archive
from test_sticker_archive import saved_mark
from test_sticker_archive_browser import archive_page as archive_page

from pack_v2.modules.printing.models import PrintJob

QZ = """(() => {
  window.__qzCalls = 0;
  window.qz = {
    security: {setCertificatePromise(){},setSignatureAlgorithm(){},setSignaturePromise(){}},
    websocket: {isActive:()=>true,connect:async()=>{}},
    printers: {find:async()=>{ if(window.__qzMissing) throw Error('Not connected'); return ['Labels','CZ']; }},
    configs: {create:(printer,options)=>({printer,options})},
    print:async(config,data)=>{ await window.capturePrint(config,data); document.documentElement.dataset.qzCalls = String(++window.__qzCalls); if(window.__qzFail) throw Error('Lost acknowledgement'); },
  };
  document.addEventListener('load',event=>{
    if(event.target.matches?.('iframe.printing-frame')) {
      event.target.contentWindow.print = () => window.capturePrint({printer:'browser'},[]);
    }
  },true);
})();"""


@pytest.fixture
def print_page(printing, archive_page):
    calls = []
    archive_page.context.expose_binding(
        "capturePrint", lambda source, config, data: calls.append((config, data))
    )
    archive_page.context.add_init_script(QZ)
    return archive_page, calls


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_settings_save_without_reload_printer_check_and_sample(printing, owner, print_page):
    page, calls = print_page
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/settings")
    expect(page.locator("[data-printer-rows] > tr")).to_have_count(5)
    page.get_by_role("button", name="Проверить", exact=True).click()
    expect(page.locator("[data-qz-status]")).to_contain_text("принтеров: 2")
    rows = page.locator("[data-printer-rows] > tr")
    rows.nth(0).get_by_label("Принтер", exact=True).select_option("Labels")
    rows.nth(0).get_by_label("Ориентация", exact=True).select_option("portrait")
    rows.filter(has_text="Дубликаты").get_by_label("Принтер", exact=True).select_option("CZ")
    rows.filter(has_text="Яндекс Маркет").get_by_label("Принтер", exact=True).select_option("Labels")
    page.locator("[data-print-product]").check()
    page.locator("[data-print-marking]").check()
    page.evaluate('window.settingsSentinel = "still here"')
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("[data-print-settings-notice]")).to_have_text("Настройки сохранены")
    assert page.evaluate("window.settingsSentinel") == "still here"
    assert printing.settings(owner)["product_barcodes"] and printing.settings(owner)["marking_duplicates"]
    page.get_by_role("button", name="Пробная печать", exact=True).first.click()
    expect(page.locator("[data-print-settings-notice]")).to_contain_text("Задание отправлено")
    assert len(calls) == 1 and calls[0][0]["printer"] == "Labels"
    assert calls[0][0]["options"]["size"] == {"width": 40, "height": 58}
    page.reload()
    expect(page.locator("[data-print-product]")).to_be_checked()
    expect(page.locator("[data-print-marking]")).to_be_checked()
    expect(
        page.locator("[data-printer-rows] > tr")
        .filter(has_text="Дубликаты")
        .get_by_label("Принтер", exact=True)
    ).to_have_value("CZ")
    expect(
        page.locator("[data-printer-rows] > tr")
        .filter(has_text="Яндекс Маркет")
        .get_by_label("Принтер", exact=True)
    ).to_have_value("Labels")
    page.screenshot(path="/tmp/ozon-pack-v2-printing-settings.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/ozon-pack-v2-printing-settings-mobile.png", full_page=True)
    assert not errors


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_auto_product_and_marking_print_once_across_reload_and_manual_reprint(
    printing, ready_pipeline, owner, db, print_page
):
    configure(printing, owner, product=True, marking=True, qz=True)
    saved_mark(ready_pipeline, owner, db)
    page, calls = print_page
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/pack")
    expect(page.locator("[data-print-status]")).to_contain_text("Задание отправлено")
    # Wait for the second document through a persistent server receipt.
    expect(page.get_by_role("button", name="Печать стикера", exact=True)).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-qz-calls", "2")
    assert len(calls) == 2
    by_printer = {config["printer"]: data for config, data in calls}
    assert set(by_printer) == {"Labels", "CZ"}
    assert len(PdfReader(io.BytesIO(base64.b64decode(by_printer["Labels"][0]["data"]))).pages) == 4
    assert len(PdfReader(io.BytesIO(base64.b64decode(by_printer["CZ"][0]["data"]))).pages) == 1
    page.reload()
    expect(page.get_by_role("button", name="Печать стикера", exact=True)).to_be_visible()
    expect(page.locator("[data-print-status]")).to_contain_text("Задание отправлено")
    assert len(calls) == 2
    page.get_by_role("button", name="Печать стикера", exact=True).click()
    expect(page.locator("html")).to_have_attribute("data-qz-calls", "1")
    expect(page.locator("[data-print-status]")).to_contain_text("Задание отправлено")
    assert len(calls) == 3 and calls[-1][0]["printer"] == "Labels"
    assert not errors


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_qz_failure_is_uncertain_without_browser_retry(printing, ready_pipeline, owner, db, print_page):
    configure(printing, owner, qz=True)
    label_job(printing, ready_pipeline, owner, db)
    page, calls = print_page
    page.add_init_script("window.__qzFail = true")
    page.goto("/pack")
    expect(page.locator("[data-print-status]")).to_contain_text("Результат печати не подтверждён")
    assert len(calls) == 1 and calls[0][0]["printer"] == "Labels"
    page.reload()
    expect(page.locator("[data-print-status]")).to_contain_text("Результат печати не подтверждён")
    assert len(calls) == 1
    with db.transaction() as tx:
        assert tx.scalar(select(PrintJob.state)) == "uncertain"


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_missing_qz_falls_back_before_dispatch_and_never_repeats_on_reload(
    printing, ready_pipeline, owner, db, print_page
):
    configure(printing, owner, qz=True)
    label_job(printing, ready_pipeline, owner, db)
    page, calls = print_page
    page.add_init_script("window.__qzMissing = true")
    page.goto("/pack")
    expect(page.locator("[data-print-status]")).to_contain_text("Открыто окно печати браузера")
    assert len(calls) == 1 and calls[0][0]["printer"] == "browser"
    page.reload()
    expect(page.locator("[data-print-status]")).to_contain_text("Открыто окно печати браузера")
    assert len(calls) == 1


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_archive_manual_label_and_marking_printing(printing, ready_pipeline, owner, db, print_page):
    configure(printing, owner, product=True, qz=True)
    saved_mark(ready_pipeline, owner, db)
    page, calls = print_page
    page.goto("/reports/stickers")
    page.get_by_role("button", name="Печать", exact=True).click()
    expect(page.locator("[data-stickers-notice]")).to_contain_text("Задание отправлено")
    page.get_by_role("button", name="Честный знак", exact=False).click()
    dialog = page.locator("[data-marking-dialog]")
    dialog.get_by_role("button", name="Печать", exact=True).click()
    expect(dialog.locator("[data-marking-print-notice]")).to_contain_text("Задание отправлено")
    assert [c[0]["printer"] for c in calls] == ["Labels", "CZ"]
    page.screenshot(path="/tmp/ozon-pack-v2-printing-marking.png", full_page=True)


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_two_tabs_cannot_dispatch_the_same_automatic_label_twice(
    printing, ready_pipeline, owner, db, print_page
):
    configure(printing, owner, qz=True)
    label_job(printing, ready_pipeline, owner, db)
    page, calls = print_page
    second = page.context.new_page()
    page.goto("/pack", wait_until="domcontentloaded")
    second.goto("/pack", wait_until="domcontentloaded")
    expect(page.locator("[data-print-status]")).to_contain_text("Задание отправлено")
    expect(second.locator("[data-print-status]")).to_contain_text("Задание отправлено")
    assert len(calls) == 1
    second.close()


@pytest.mark.skipif(not shutil.which("chromium"), reason="System Chromium required")
def test_lost_start_response_never_reaches_local_printer_and_does_not_restart(
    printing, ready_pipeline, owner, db, print_page, client
):
    configure(printing, owner, qz=True)
    label_job(printing, ready_pipeline, owner, db)
    page, calls = print_page

    def lose_response(route):
        req = route.request
        response = client.request(req.method, req.url, headers=req.headers, content=req.post_data_buffer)
        assert response.json()["dispatch"]
        route.abort("connectionfailed")

    page.route("**/api/v2/printing/jobs/*/start", lose_response)
    page.goto("/pack")
    expect(page.locator("[data-print-status]")).to_contain_text("Результат печати не подтверждён")
    assert not calls
    page.unroute("**/api/v2/printing/jobs/*/start")
    page.reload()
    expect(page.locator("[data-print-status]")).to_contain_text("Результат печати не подтверждён")
    assert not calls
