"""Наборы: из чего физически собирается товар площадки.

На Ozon набор — обычный товар: один SKU, один артикул, один штрихкод. В
отправлении он стоит одной позицией, и площадка не знает, что на складе его
собирают из нескольких разных вещей со своими штрихкодами.

Отсюда и задача. Сборщик берёт с полки части набора и сканирует их — штрихкод
набора он отсканировать не может, потому что такой наклейки на полке нет.
Панель на такой скан отвечала «СТОП: товар не из этого отправления»: SKU части
в составе отправления не значится. Состав набора эту дыру и закрывает.

Состав хранится только в панели. Площадке его не отправить: у Ozon для этого
ничего нет, да и незачем — собирают на складе, а не на площадке.

Часть набора — либо товар площадки (тогда у неё есть SKU и своё название),
либо просто штрихкод. Второе нужно, потому что не всё, что лежит в наборе,
продаётся отдельно: вкладыш, пакет, подарок из другой поставки.
"""
from __future__ import annotations

from . import db

# Ключ части внутри набора. У товара площадки это его SKU, у штрихкода без
# товара — префикс и сам код. По этому ключу считается прогресс сборки, поэтому
# он обязан пережить и переименование товара, и обновление каталога.
BARCODE_PREFIX = "bc:"

MAX_PARTS = 50


class SetError(ValueError):
    """Набор нельзя сохранить — причина написана человеку."""


def part_key(sku: str | None, barcode: str | None) -> str:
    return str(sku) if sku else BARCODE_PREFIX + str(barcode or "").strip()


# ------------------------------------------------------------------ чтение
def parts_of(account_id: int, set_sku: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM product_set_items WHERE account_id = ? AND set_sku = ? "
        "ORDER BY sort, part_key",
        (account_id, set_sku),
    )
    return [dict(row) for row in rows]


def get(account_id: int, set_sku: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM product_sets WHERE account_id = ? AND sku = ?", (account_id, set_sku)
    )
    if not row:
        return None
    return {**dict(row), "parts": parts_of(account_id, set_sku)}


def all_sets(account_id: int) -> list[dict]:
    """Все наборы кабинета вместе с составом — для экрана «Товары»."""
    rows = db.query(
        "SELECT s.*, p.name AS product_name, p.offer_id, p.image "
        "FROM product_sets s LEFT JOIN products p ON p.account_id = s.account_id AND p.sku = s.sku "
        "WHERE s.account_id = ? ORDER BY COALESCE(s.title, p.name, s.sku)",
        (account_id,),
    )
    result = []
    for row in rows:
        item = dict(row)
        item["parts"] = parts_of(account_id, item["sku"])
        item["parts_total"] = sum(int(part["quantity"] or 1) for part in item["parts"])
        result.append(item)
    return result


def all_sets_everywhere(account_ids: list[int] | None = None) -> list[dict]:
    """Наборы всех кабинетов сразу — раздел «Товары» общий на всю панель.

    Набор остаётся набором кабинета: состав задаётся для карточки площадки,
    которая продаётся как набор. Но искать его по кабинетам, переключаясь между
    ними, незачем — на складе они стоят на одной полке.
    """
    conditions = ["1 = 1"]
    params: list = []
    if account_ids:
        marks = ",".join("?" for _ in account_ids)
        conditions.append(f"s.account_id IN ({marks})")
        params += list(account_ids)
    rows = db.query(
        f"SELECT s.*, p.name AS product_name, p.offer_id, p.image, "
        f"       a.title AS shop, a.marketplace AS market "
        f"  FROM product_sets s "
        f"  JOIN accounts a ON a.id = s.account_id "
        f"  LEFT JOIN products p ON p.account_id = s.account_id AND p.sku = s.sku "
        f" WHERE {' AND '.join(conditions)} "
        f" ORDER BY COALESCE(s.title, p.name, s.sku)",
        params,
    )
    result = []
    for row in rows:
        item = dict(row)
        item["parts"] = parts_of(item["account_id"], item["sku"])
        item["parts_total"] = sum(int(part["quantity"] or 1) for part in item["parts"])
        result.append(item)
    return result


def set_skus(account_id: int) -> set[str]:
    """SKU, у которых есть рабочий состав. Пустой набор набором не считается."""
    rows = db.query(
        "SELECT DISTINCT s.sku FROM product_sets s "
        "JOIN product_set_items i ON i.account_id = s.account_id AND i.set_sku = s.sku "
        "WHERE s.account_id = ? AND s.active = 1",
        (account_id,),
    )
    return {row["sku"] for row in rows}


def parts_for(account_id: int, skus: list[str]) -> dict[str, list[dict]]:
    """Состав сразу для нескольких SKU — чтобы не спрашивать базу по строке.

    Отдаём только рабочие наборы: выключенный или пустой набор собирается как
    обычный товар, по своему штрихкоду.
    """
    if not skus:
        return {}
    placeholders = ",".join("?" for _ in skus)
    rows = db.query(
        f"""
        SELECT i.*, p.name AS product_name, p.image, p.offer_id
        FROM product_set_items i
        JOIN product_sets s ON s.account_id = i.account_id AND s.sku = i.set_sku AND s.active = 1
        LEFT JOIN products p ON p.account_id = i.account_id AND p.sku = i.part_sku
        WHERE i.account_id = ? AND i.set_sku IN ({placeholders})
        ORDER BY i.sort, i.part_key
        """,
        [account_id] + list(skus),
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["set_sku"], []).append(dict(row))
    return grouped


def parents_of(account_id: int, *, sku: str | None = None, barcodes: list[str] | None = None) -> list[dict]:
    """В какие наборы входит отсканированное. Обычно ровно в один.

    Ищем и по SKU (часть есть в каталоге площадки), и по штрихкоду (части в
    каталоге нет). Одна часть может входить в несколько наборов — какой из них
    засчитать, решает состав отправления, а не эта функция.
    """
    conditions, params = [], [account_id]
    if sku:
        conditions.append("i.part_sku = ?")
        params.append(sku)
    codes = [code for code in (barcodes or []) if code]
    if codes:
        placeholders = ",".join("?" for _ in codes)
        conditions.append(f"i.barcode IN ({placeholders})")
        params += codes
    if not conditions:
        return []
    rows = db.query(
        f"""
        SELECT i.* FROM product_set_items i
        JOIN product_sets s ON s.account_id = i.account_id AND s.sku = i.set_sku AND s.active = 1
        WHERE i.account_id = ? AND ({' OR '.join(conditions)})
        """,
        params,
    )
    return [dict(row) for row in rows]


def part_title(part: dict) -> str:
    return (part.get("title") or part.get("product_name")
            or part.get("barcode") or part.get("part_sku") or "Часть набора")


# ------------------------------------------------------------------ запись
def save(account_id: int, set_sku: str, parts: list[dict], *, title: str = "",
         user: dict | None = None) -> dict:
    """Создать или переписать набор целиком.

    Состав задаётся списком, а не правками по одной части: так на экране видно
    ровно то, что сохранится, и не бывает набора, у которого половина состава
    от старой версии.
    """
    set_sku = str(set_sku or "").strip()
    if not set_sku:
        raise SetError("Не выбран товар-набор")
    product = db.query_one(
        "SELECT sku, name FROM products WHERE account_id = ? AND sku = ?", (account_id, set_sku)
    )
    if not product:
        raise SetError(f"Товара с SKU {set_sku} нет в каталоге кабинета")

    cleaned = _clean_parts(account_id, set_sku, parts)
    now = db.now_iso()
    with db.write() as conn:
        existing = conn.execute(
            "SELECT created_at, created_by FROM product_sets WHERE account_id = ? AND sku = ?",
            (account_id, set_sku),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO product_sets(account_id, sku, title, active, created_at, created_by, updated_at)
            VALUES(?,?,?,1,?,?,?)
            ON CONFLICT(account_id, sku) DO UPDATE SET
                title = excluded.title, active = 1, updated_at = excluded.updated_at
            """,
            (account_id, set_sku, title.strip() or None,
             (existing["created_at"] if existing else now) or now,
             (existing["created_by"] if existing else (user or {}).get("login")), now),
        )
        conn.execute(
            "DELETE FROM product_set_items WHERE account_id = ? AND set_sku = ?", (account_id, set_sku)
        )
        for sort, part in enumerate(cleaned):
            conn.execute(
                "INSERT INTO product_set_items(account_id, set_sku, part_key, part_sku, barcode, "
                "title, quantity, sort) VALUES(?,?,?,?,?,?,?,?)",
                (account_id, set_sku, part["part_key"], part["part_sku"], part["barcode"],
                 part["title"], part["quantity"], sort),
            )
    db.log_event(
        "product_set_saved", account_id=account_id, user=user, sku=set_sku,
        message=f"{title.strip() or product['name'] or set_sku}: частей {len(cleaned)}",
    )
    return get(account_id, set_sku)


def _clean_parts(account_id: int, set_sku: str, parts: list[dict]) -> list[dict]:
    """Разобрать состав: найти товары по штрихкоду, проверить и сложить повторы."""
    if not parts:
        raise SetError("В наборе должна быть хотя бы одна часть")
    if len(parts) > MAX_PARTS:
        raise SetError(f"Частей в наборе не больше {MAX_PARTS}")

    nested = set_skus(account_id)
    merged: dict[str, dict] = {}
    for raw in parts:
        sku = str(raw.get("sku") or "").strip()
        barcode = str(raw.get("barcode") or "").strip()
        if not sku and not barcode:
            continue
        try:
            quantity = int(raw.get("quantity") or 1)
        except (TypeError, ValueError):
            raise SetError("Количество части — целое число") from None
        if quantity < 1:
            raise SetError("Количество части не меньше единицы")

        if not sku and barcode:
            # Штрихкод знакомого товара привязываем к товару: так у части будет
            # название и фото, а не голый код на экране сборщика.
            known = db.query_one(
                "SELECT sku FROM product_barcodes WHERE account_id = ? AND barcode = ?",
                (account_id, barcode),
            )
            if known:
                sku = known["sku"]
        if sku and sku == set_sku:
            raise SetError("Набор не может состоять из самого себя")
        if sku and sku in nested:
            raise SetError(
                f"«{_name_of(account_id, sku)}» сам является набором. "
                "Вложенные наборы не поддерживаются: перечислите части напрямую."
            )

        key = part_key(sku, barcode)
        if key in merged:
            merged[key]["quantity"] += quantity
            continue
        merged[key] = {
            "part_key": key,
            "part_sku": sku or None,
            "barcode": barcode or None,
            "title": str(raw.get("title") or "").strip() or (_name_of(account_id, sku) if sku else None),
            "quantity": quantity,
        }
    if not merged:
        raise SetError("В наборе должна быть хотя бы одна часть")
    return list(merged.values())


def _name_of(account_id: int, sku: str) -> str | None:
    row = db.query_one("SELECT name FROM products WHERE account_id = ? AND sku = ?", (account_id, sku))
    return row["name"] if row else None


def delete(account_id: int, set_sku: str, user: dict | None = None) -> bool:
    """Убрать набор. Товар остаётся обычным: собирается по своему штрихкоду."""
    with db.write() as conn:
        removed = conn.execute(
            "DELETE FROM product_sets WHERE account_id = ? AND sku = ?", (account_id, set_sku)
        ).rowcount or 0
        conn.execute(
            "DELETE FROM product_set_items WHERE account_id = ? AND set_sku = ?", (account_id, set_sku)
        )
    if removed:
        db.log_event("product_set_deleted", account_id=account_id, user=user, sku=set_sku,
                     message=f"Набор {set_sku} удалён")
    return bool(removed)
