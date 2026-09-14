"""Акты выдачи возвратов, которые составляет Ozon.

Это основной источник раздела «Ждёт подтверждения». В пункте выдачи забирают
всё разом — и FBS, и FBO, — и Ozon составляет на это акт: что отдано и когда.
Панель забирает акт методами /v1/return/giveout/* и раскладывает его на строки
своих возвратов. Так состав и время берутся у площадки, а не выводятся из того,
что возврат перестал показываться к выдаче.

Строки акта ложатся на возвраты по штрихкоду. Ищем его по значению, а не по
имени колонки: поля этого метода у Ozon менялись, и привязка к имени сломалась
бы на следующей версии. Ответ целиком всё равно сохраняется в raw — если что-то
разъедется, будет по чему чинить.

У Avito такого документа нет вовсе: его возвраты попадают в акт только вместе
с общим листом, и отметки по ним ставятся так же.
"""
from __future__ import annotations

import json
import logging

from . import db, ozon, return_acts, store
from .ozon import OzonError

log = logging.getLogger("giveouts")

MAX_PAGES = 20
PAGE_LIMIT = 100

# Как Ozon называет одно и то же в разных версиях ответа.
ID_KEYS = ("giveout_id", "id")
STATUS_KEYS = ("giveout_status", "status", "state")
CREATED_KEYS = ("created_at", "created", "giveout_date", "date")
ITEM_LIST_KEYS = ("articles", "items", "products", "returns")

STATUS_LABELS = {
    "FORMED": "Сформирован",
    "CREATED": "Сформирован",
    "NEW": "Сформирован",
    "IN_PROGRESS": "В работе",
    "APPROVED": "Подтверждён",
    "COMPLETED": "Выдан",
    "DONE": "Выдан",
    "CANCELLED": "Отменён",
    "CANCELED": "Отменён",
    "EXPIRED": "Просрочен",
}


def _pick(raw: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = raw.get(name)
        if value not in (None, "", []):
            return str(value)
    return ""


def _items_of(info: dict) -> list[dict]:
    for name in ITEM_LIST_KEYS:
        value = info.get(name)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def status_label(code: str) -> str:
    return STATUS_LABELS.get((code or "").upper(), code or "")


def _values(item: dict) -> list[str]:
    """Все строковые значения строки акта, включая вложенные."""
    found: list[str] = []
    stack = [item]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, (str, int)) and not isinstance(current, bool):
            text = str(current).strip()
            if text:
                found.append(text)
    return found


def match_returns(account_id: int, items: list[dict]) -> list[str]:
    """Возвраты кабинета, которые описаны строками акта.

    Сопоставляем по значению, а не по имени поля: в акте есть штрихкод
    возврата, но как именно называется колонка — у метода менялось. Берём из
    строки все значения и ищем совпадение среди штрихкодов, идентификаторов и
    номеров отправлений этого кабинета. Такой поиск не зависит от того, как
    Ozon назовёт поле в следующей версии.
    """
    candidates: set[str] = set()
    for item in items:
        candidates.update(_values(item))
    if not candidates:
        return []

    found: list[str] = []
    values = list(candidates)
    for start in range(0, len(values), 400):
        chunk = values[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.query(
            f"SELECT id FROM returns WHERE account_id = ? AND ("
            f"barcode IN ({placeholders}) OR id IN ({placeholders}) OR posting_number IN ({placeholders}))",
            [account_id] + chunk * 3,
        )
        found += [row["id"] for row in rows]
    # Порядок не важен, важна однократность: один возврат — одна строка акта.
    return sorted(set(found))


def act_from_document(account: dict, user: dict | None = None, *, dry_run: bool = False) -> dict:
    """Забрать документ выдачи у Ozon и собрать по нему акт.

    Второй путь получения акта, без участия человека: /v1/return/giveout/get-pdf
    отдаёт сам документ, а разбираем его тем же способом, что и загруженный
    файл — по содержимому. Помогает там, где /v1/return/giveout/list выключен
    или отдаёт состав в незнакомых полях: в документе штрихкоды всё равно
    напечатаны.

    Документ у Ozon один — текущая активная выдача. Поэтому повторный вызов
    безвреден: возвраты, уже разложенные по актам, второй раз не заберутся, и
    пустой акт не появится.
    """
    from . import act_upload, return_acts

    try:
        # get_client тоже кидает OzonError — у кабинета может не быть ключей.
        # Это обычное состояние панели, и отвечать на него 500 нельзя.
        data = ozon.get_client(account).giveout_pdf()
    except OzonError as exc:
        return {"status": "error", "message": f"Ozon не отдал документ выдачи: {exc.message}"}

    try:
        found = act_upload.parse("giveout.pdf", data)
    except act_upload.UploadRejected as exc:
        return {"status": "error", "message": str(exc)}

    return_ids = match_returns(account["id"], [{"code": code} for code in found])
    if not return_ids:
        return {
            "status": "warning",
            "found": 0,
            "codes": len(found),
            "message": f"В документе выдачи {len(found)} кодов, но ни один не совпал с возвратами "
                       f"кабинета «{account['title']}».",
        }
    if dry_run:
        return {"status": "ok", "found": len(return_ids), "codes": len(found),
                "message": f"В документе выдачи Ozon нашлось возвратов: {len(return_ids)}"}

    act_id = return_acts.from_upload(
        account["id"], user or {"login": "синхронизация"},
        source="документ выдачи Ozon", return_ids=return_ids,
    )
    if not act_id:
        return {"status": "warning", "found": len(return_ids),
                "message": "Все возвраты из документа выдачи уже разнесены по актам"}
    return {"status": "ok", "found": len(return_ids), "act_id": act_id,
            "message": f"Акт собран по документу Ozon: {len(return_ids)} возвратов"}


# Документ выдачи — отдельный запрос к Ozon, и дёргать его каждую минуту не за
# чем: активная выдача меняется куда реже. Пробуем не чаще, чем раз в столько.
DOCUMENT_RETRY_MINUTES = 15


def _document_is_due(account_id: int) -> bool:
    from datetime import datetime, timedelta, timezone

    key = f"giveout_doc_try_{account_id}"
    last = db.kv_get(key)
    if last:
        try:
            moment = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment and datetime.now(timezone.utc) - moment < timedelta(minutes=DOCUMENT_RETRY_MINUTES):
            return False
    db.kv_set(key, db.now_iso())
    return True


def sync_account(account: dict) -> dict:
    """Загрузить акты выдачи кабинета. Мягко к отсутствию метода.

    Метод выдачи включён не у всех продавцов, и старые кабинеты отвечают на
    него ошибкой. Это не повод ронять синхронизацию возвратов: возвращаем
    причину, панель её показывает.
    """
    client = ozon.get_client(account)
    account_id = account["id"]
    saved = 0
    matched = 0
    unmatched = 0
    last_id = 0

    for _page in range(MAX_PAGES):
        try:
            giveouts, has_next = client.giveout_list(limit=PAGE_LIMIT, last_id=last_id)
        except OzonError as exc:
            log.warning("Акты выдачи недоступны: %s", exc)
            # Списка нет — документ выдачи может быть, и штрихкоды в нём есть.
            if _document_is_due(account_id):
                from_document = act_from_document(account)
                if from_document.get("found"):
                    return _result(saved, from_document["found"], unmatched,
                                   error=exc.message, document=from_document["message"])
            return _result(saved, matched, unmatched, error=exc.message)
        if not giveouts:
            break
        for raw in giveouts:
            giveout_id = _pick(raw, ID_KEYS)
            if not giveout_id:
                continue
            info: dict = {}
            try:
                info = client.giveout_info(giveout_id)
            except OzonError as exc:
                # Список дошёл, состав — нет. Акт всё равно сохраняем: сам факт
                # выдачи важнее её состава.
                log.info("Состав акта %s недоступен: %s", giveout_id, exc)
            merged = {**raw, **info}
            items = _items_of(merged)
            _upsert(account_id, giveout_id, merged, items)
            saved += 1
            # Строки акта — это и есть «Ждёт подтверждения»: площадка сама
            # сказала, какие возвраты отдала и когда.
            return_ids = match_returns(account_id, items)
            return_acts.from_giveout(
                account_id, giveout_id,
                created_at=_pick(merged, CREATED_KEYS) or None,
                status=_pick(merged, STATUS_KEYS) or None,
                return_ids=return_ids,
            )
            matched += len(return_ids)
            if items and not return_ids:
                # Акт есть, а возвраты по нему не нашлись: у нас нет их строк
                # или в акте нет ни штрихкода, ни номера. Молчать нельзя —
                # иначе раздел останется пустым без объяснения.
                unmatched += 1
                log.info("Акт %s: ни один из %s товаров не сопоставлен с возвратами",
                         giveout_id, len(items))
        last_id = _pick(giveouts[-1], ID_KEYS) or 0
        try:
            last_id = int(last_id)
        except (TypeError, ValueError):
            break
        if not has_next or not last_id:
            break

    # Список актов ничего полезного не дал — пробуем сам документ выдачи. Там
    # штрихкоды напечатаны, и он работает даже когда список выключен или отдаёт
    # состав в незнакомых полях.
    if not matched and _document_is_due(account_id):
        from_document = act_from_document(account)
        if from_document.get("found"):
            matched += from_document["found"]
            return _result(saved, matched, unmatched, document=from_document["message"])

    return _result(saved, matched, unmatched)


def _result(saved: int, matched: int, unmatched: int, *, error: str | None = None,
            document: str | None = None) -> dict:
    result: dict = {"giveouts": saved}
    if matched:
        result["giveouts_returns"] = matched
    if unmatched:
        result["giveouts_unmatched"] = unmatched
    if document:
        result["giveouts_document"] = document
    if error:
        result["giveouts_error"] = error
    return result


def _upsert(account_id: int, giveout_id: str, raw: dict, items: list[dict]) -> None:
    now = db.now_iso()
    status = _pick(raw, STATUS_KEYS)
    existing = db.query_one(
        "SELECT first_seen_at FROM ozon_giveouts WHERE account_id = ? AND id = ?", (account_id, giveout_id)
    )
    db.execute(
        """
        INSERT INTO ozon_giveouts(account_id, id, status, status_label, created_at, items_count,
                                  items, raw, first_seen_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            status = excluded.status, status_label = excluded.status_label,
            created_at = excluded.created_at, items_count = excluded.items_count,
            items = excluded.items, raw = excluded.raw, updated_at = excluded.updated_at
        """,
        (
            account_id, giveout_id, status or None, status_label(status) or None,
            _pick(raw, CREATED_KEYS) or None, len(items),
            json.dumps(items, ensure_ascii=False),
            json.dumps(raw, ensure_ascii=False),
            (existing["first_seen_at"] if existing else now) or now,
            now,
        ),
    )


def view(row) -> dict:
    data = dict(row)
    data.pop("raw", None)
    try:
        data["items"] = json.loads(data.get("items") or "[]")
    except ValueError:
        data["items"] = []
    data["created_local"] = store.local_time(data.get("created_at"))
    data["status_label"] = data.get("status_label") or status_label(data.get("status") or "")
    return data


def recent(account_id: int, limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT * FROM ozon_giveouts WHERE account_id = ? "
        "ORDER BY (created_at IS NULL), created_at DESC, first_seen_at DESC LIMIT ?",
        (account_id, limit),
    )
    return [view(row) for row in rows]


def raw_of(account_id: int, giveout_id: str) -> dict | None:
    """Ответ Ozon по акту как есть — чтобы видеть, что площадка реально прислала."""
    row = db.query_one(
        "SELECT raw FROM ozon_giveouts WHERE account_id = ? AND id = ?", (account_id, giveout_id)
    )
    if not row:
        return None
    try:
        return json.loads(row["raw"] or "{}")
    except ValueError:
        return {}
