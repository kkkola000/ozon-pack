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


def available(account: dict, days: int = 7) -> dict:
    """Акты выдачи Ozon за последние дни — список на выбор.

    Показываем, что у площадки есть, и сколько возвратов кабинета в каждом акте
    узнаётся. Дальше администратор отмечает нужные и добавляет их в панель:
    решать, какие поездки заводить, ему, а не синхронизации.
    """
    from datetime import datetime, timedelta, timezone

    try:
        raw_acts, _ = ozon.get_client(account).giveout_list(limit=PAGE_LIMIT)
    except OzonError as exc:
        return {"status": "error", "message": f"Ozon не отдал список актов: {exc.message}"}

    since = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    taken = {
        row["giveout_id"]
        for row in db.query("SELECT giveout_id FROM return_acts WHERE giveout_id IS NOT NULL")
    }

    found = []
    for raw in raw_acts:
        giveout_id = _pick(raw, ID_KEYS)
        if not giveout_id:
            continue
        created = _pick(raw, CREATED_KEYS)
        if created and not _within(created, since):
            continue
        info: dict = {}
        try:
            info = ozon.get_client(account).giveout_info(giveout_id)
        except OzonError as exc:
            log.info("Состав акта %s недоступен: %s", giveout_id, exc)
        merged = {**raw, **info}
        items = _items_of(merged)
        _upsert(account["id"], giveout_id, merged, items)
        matched = match_returns(account["id"], items)
        # Возврат, уже лежащий в чужом подтверждённом акте, второй раз не
        # заберётся — показываем только то, что реально добавится.
        free = return_acts.free_returns(account["id"], matched)
        found.append({
            "id": giveout_id,
            "created_at": created,
            "created_local": store.local_time(created),
            "status": _pick(merged, STATUS_KEYS),
            "status_label": status_label(_pick(merged, STATUS_KEYS)),
            "items": len(items),
            "matched": len(matched),
            "free": len(free),
            "in_panel": giveout_id in taken,
            "names": [
                (item.get("article_name") or item.get("name") or item.get("product_name") or "—")
                for item in items[:5]
            ],
        })

    found.sort(key=lambda act: act["created_at"] or "", reverse=True)
    return {"status": "ok", "days": days, "acts": found}


def _within(created: str, since) -> bool:
    from datetime import datetime, timezone

    try:
        moment = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        # Дату не разобрали — не прячем акт: пусть человек сам решит.
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= since


def import_acts(account: dict, user: dict, giveout_ids: list[str]) -> dict:
    """Завести в панели выбранные акты выдачи."""
    added = 0
    returns = 0
    skipped = []
    for giveout_id in giveout_ids:
        # Акт уже заведён: повторный импорт не добавляет работы, он только
        # обновил бы статус. Считать его добавленным нельзя — человек решит,
        # что завёл поездку, которой в списке нет.
        if db.query_one("SELECT id FROM return_acts WHERE giveout_id = ?", (giveout_id,)):
            skipped.append(giveout_id)
            continue
        row = db.query_one(
            "SELECT * FROM ozon_giveouts WHERE account_id = ? AND id = ?", (account["id"], giveout_id)
        )
        if not row:
            skipped.append(giveout_id)
            continue
        act = view(row)
        matched = match_returns(account["id"], act["items"])
        act_id = return_acts.from_giveout(
            account["id"], giveout_id, created_at=act.get("created_at"),
            status=act.get("status"), return_ids=matched,
        )
        if act_id:
            added += 1
            returns += len(matched)
        else:
            skipped.append(giveout_id)

    if added:
        db.log_event(
            "return_act_import", account_id=account["id"], user=user,
            message=f"актов: {added}, возвратов: {returns}",
        )
    if added:
        message = f"Добавлено актов: {added}, возвратов в них: {returns}"
    else:
        message = ("Ни один акт не добавлен: они уже в панели, "
                   "либо их возвраты разнесены по другим актам или не опознаны")
    return {"status": "ok" if added else "warning", "added": added, "returns": returns,
            "skipped": skipped, "message": message}


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
    from . import act_upload

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
            # Акт в панель не заводим: какие поездки открыть — решает человек
            # во вкладке «Ждёт подтверждения». Синхронизация только приносит
            # список, чтобы было из чего выбирать.
            return_ids = match_returns(account_id, items)
            matched += len(return_ids)
            if items and not return_ids:
                # Возвраты по акту не опознаны: их нет в панели или в акте нет
                # ни штрихкода, ни номера. Молчать нельзя — иначе выбирать
                # будет не из чего и непонятно почему.
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
