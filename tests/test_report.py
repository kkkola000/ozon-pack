"""Отчёт об отгруженных товарах: когда появляется строка и что в ней."""
from datetime import datetime, timedelta, timezone

import pytest

from app import db, packing, report, store
from tests.conftest import barcode_of, pick_posting


def shipped_rows(status=None):
    sql = "SELECT * FROM shipped_items"
    params = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    return [dict(r) for r in db.query(sql + " ORDER BY id", params)]


# ------------------------------------------------------------------ отсечка дня
@pytest.mark.parametrize(
    "utc,expected",
    [
        # TZ_OFFSET_HOURS = 3, отсечка 18:00 -> 15:00 UTC
        ("2026-09-06T14:59:00+00:00", "2026-09-06"),
        ("2026-09-06T15:00:00+00:00", "2026-09-07"),
        ("2026-09-06T23:00:00+00:00", "2026-09-07"),
        # 00:30 МСК седьмого — до отсечки, значит день седьмой
        ("2026-09-06T21:30:00+00:00", "2026-09-07"),
        ("2026-09-07T05:00:00+00:00", "2026-09-07"),
    ],
)
def test_report_day_respects_cutoff(utc, expected):
    assert report.report_date(utc) == expected


def test_cutoff_can_be_changed_and_validated():
    assert report.set_cutoff("20:30") == "20:30"
    assert report.report_date("2026-09-06T15:00:00+00:00") == "2026-09-06"
    with pytest.raises(ValueError):
        report.set_cutoff("25:99")
    with pytest.raises(ValueError):
        report.set_cutoff("вечером")
    # плохое значение не затёрло рабочее
    assert report.get_cutoff() == "20:30"


def test_day_is_closed_only_after_cutoff():
    today = report.report_date()
    yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
    assert report.is_closed(yesterday) is True
    assert report.is_closed(today) is False


# ------------------------------------------------------------------ запись строк
def test_row_appears_only_when_pair_matches(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]

    # Открыли отправление без скана товара — в отчёте пусто
    packing.select_posting(account, user, posting["posting_number"])
    assert shipped_rows() == []

    packing.scan(account, user, barcode_of(sku))
    rows = shipped_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["posting_number"] == posting["posting_number"]
    assert row["sku"] == sku
    assert row["barcode"] == barcode_of(sku)
    assert row["login"] == user["login"]
    assert row["unit_no"] == 1
    assert row["report_date"] == report.report_date()


def test_first_scan_that_picks_posting_is_recorded(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    result = packing.scan(account, user, barcode_of(sku))
    if result["action"] == "need_choice":
        packing.select_posting(account, user, posting["posting_number"], first_sku=sku)

    rows = shipped_rows("ok")
    assert len(rows) == 1
    assert rows[0]["sku"] == sku


def test_every_unit_gets_its_own_row(account, sample_data, user):
    posting = db.query_one(
        """
        SELECT p.posting_number, i.sku, i.quantity FROM postings p
        JOIN posting_items i ON i.posting_number = p.posting_number AND i.account_id = p.account_id
        WHERE p.account_id = ? AND p.status = ? AND p.local_state = 'new' AND i.quantity > 1
        LIMIT 1
        """,
        (account["id"], store.STATUS_AWAITING_DELIVER),
    )
    if not posting:
        pytest.skip("в тестовых данных нет позиции с количеством больше одного")

    packing.select_posting(account, user, posting["posting_number"])
    for _ in range(posting["quantity"]):
        packing.scan(account, user, barcode_of(posting["sku"]))

    rows = shipped_rows("ok")
    assert len(rows) == posting["quantity"]
    assert [r["unit_no"] for r in rows] == list(range(1, posting["quantity"] + 1))


def test_wrong_product_is_recorded_as_error(account, sample_data, user):
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])
    foreign = db.query_one(
        "SELECT sku FROM posting_items WHERE account_id = ? AND sku NOT IN "
        "(SELECT sku FROM posting_items WHERE account_id = ? AND posting_number = ?) LIMIT 1",
        (account["id"], account["id"], posting["posting_number"]),
    )["sku"]

    packing.scan(account, user, barcode_of(foreign))
    rows = shipped_rows("error")
    assert len(rows) == 1
    assert rows[0]["reason"] == "wrong_product"
    assert rows[0]["posting_number"] == posting["posting_number"]
    assert rows[0]["sku"] == foreign
    # по артикулу продавца потом и разбирают, что именно взяли
    assert rows[0]["offer_id"]
    # ошибка — не единица товара, порядкового номера у неё нет
    assert rows[0]["unit_no"] == 0
    # ошибка не считается отгрузкой
    assert shipped_rows("ok") == []


def test_wrong_label_is_recorded_as_error(account, sample_data, user):
    first = pick_posting(positions=1)
    other = db.query_one(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status = ? "
        "AND local_state = 'new' AND posting_number != ? LIMIT 1",
        (account["id"], store.STATUS_AWAITING_DELIVER, first["posting_number"]),
    )["posting_number"]

    packing.select_posting(account, user, first["posting_number"])
    packing.scan(account, user, other)

    rows = shipped_rows("error")
    assert len(rows) == 1
    assert rows[0]["reason"] == "wrong_label"
    assert rows[0]["posting_number"] == first["posting_number"]


def test_unknown_barcode_is_recorded_with_posting_and_name(account, sample_data, user):
    """У Avito штрихкодов нет — пишем что отсканировали, куда и что за товар."""
    posting = pick_posting(positions=1)
    packing.select_posting(account, user, posting["posting_number"])

    packing.scan(account, user, "НЕИЗВЕСТНЫЙ-КОД-12345")
    rows = shipped_rows("unmatched")
    assert len(rows) == 1
    assert rows[0]["barcode"] == "НЕИЗВЕСТНЫЙ-КОД-12345"
    assert rows[0]["posting_number"] == posting["posting_number"]
    assert rows[0]["name"] == posting["items"][0]["name"]


# ------------------------------------------------------------------ дубли
def test_repeated_collection_does_not_duplicate_rows(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    packing.select_posting(account, user, posting["posting_number"])
    packing.scan(account, user, barcode_of(sku))
    assert len(shipped_rows("ok")) == 1

    # Отправление вернули в работу и собрали заново — строка остаётся одна
    db.execute(
        "UPDATE postings SET local_state = 'new', packed_at = NULL, packed_by = NULL "
        "WHERE account_id = ? AND posting_number = ?",
        (account["id"], posting["posting_number"]),
    )
    packing.release(account, user)
    packing.select_posting(account, user, posting["posting_number"])
    packing.scan(account, user, barcode_of(sku))
    assert len(shipped_rows("ok")) == 1


def test_extra_scan_is_error_not_shipment(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    need = posting["items"][0]["quantity"]
    packing.select_posting(account, user, posting["posting_number"])
    for _ in range(need + 2):
        packing.scan(account, user, barcode_of(sku))

    assert len(shipped_rows("ok")) == need
    errors = shipped_rows("error")
    assert len(errors) == 2
    assert {e["reason"] for e in errors} == {"extra_product"}


# ------------------------------------------------------------------ выгрузка
def test_days_and_csv(account, sample_data, user):
    posting = pick_posting(positions=1)
    sku = posting["items"][0]["sku"]
    packing.select_posting(account, user, posting["posting_number"])
    packing.scan(account, user, barcode_of(sku))

    today = report.report_date()
    days = report.days()
    assert days and days[0]["day"] == today
    assert days[0]["shipped"] == 1
    assert days[0]["closed"] is False

    blob = report.to_csv(today)
    assert blob.startswith("﻿".encode("utf-8")), "без BOM Excel показывает кракозябры"
    text = blob.decode("utf-8-sig")
    lines = [line for line in text.splitlines() if line]
    assert lines[0].startswith("Отчётный день;")
    assert len(lines) == 2
    assert posting["posting_number"] in lines[1]
    assert ";Отгружен;" in lines[1]


# ------------------------------------------------------------------ страницы
class TestPages:
    """Раздел отчётов виден только администратору и отдаёт CSV."""

    @staticmethod
    def _client():
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app, follow_redirects=False)

    @staticmethod
    def _login(client, login_name="admin", password="test-admin-pass"):
        import re

        response = client.post("/login", data={"login": login_name, "password": password, "next": "/pack"})
        assert response.status_code == 303, response.text
        page = client.get("/pack")
        match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
        return match.group(1) if match else ""

    def test_admin_sees_report_pages(self, account, sample_data, user):
        posting = pick_posting(positions=1)
        packing.select_posting(account, user, posting["posting_number"])
        packing.scan(account, user, barcode_of(posting["items"][0]["sku"]))
        day = report.report_date()

        with self._client() as client:
            self._login(client)
            page = client.get("/reports")
            assert page.status_code == 200
            assert day in page.text

            detail = client.get(f"/reports/{day}")
            assert detail.status_code == 200
            assert posting["posting_number"] in detail.text

            csv_file = client.get(f"/reports/{day}/csv")
            assert csv_file.status_code == 200
            assert "attachment" in csv_file.headers["content-disposition"]
            assert f"otgruzka-{day}.csv" in csv_file.headers["content-disposition"]
            assert csv_file.content.startswith("﻿".encode("utf-8"))
            assert posting["posting_number"] in csv_file.content.decode("utf-8-sig")

    def test_packer_has_no_access(self, account, sample_data, other_user):
        with self._client() as client:
            self._login(client, "petrov", "secret123")
            assert client.get("/reports").status_code == 403
            assert client.get(f"/reports/{report.report_date()}/csv").status_code == 403

    def test_bad_date_is_404(self, account, sample_data, user):
        with self._client() as client:
            self._login(client)
            assert client.get("/reports/не-дата").status_code == 404

    def test_cutoff_is_saved_from_settings(self, account, sample_data, user):
        with self._client() as client:
            csrf = self._login(client)
            response = client.post(
                "/api/report/cutoff", json={"cutoff": "19:45"}, headers={"X-CSRF-Token": csrf}
            )
            assert response.status_code == 200, response.text
            assert report.get_cutoff() == "19:45"

            bad = client.post(
                "/api/report/cutoff", json={"cutoff": "полдень"}, headers={"X-CSRF-Token": csrf}
            )
            assert bad.status_code == 400
            assert report.get_cutoff() == "19:45"
