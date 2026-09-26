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

Набор действует на всю группу сопоставленных карточек (core/linked.py). Состав
задают одной карточке, а собирают по нему заказы всех: коробка на полке одна,
из какого бы магазина ни пришёл заказ. Часть узнаётся и по штрихкоду
сопоставленной с ней карточки — наклейка на полке может быть любой площадки.
"""
from __future__ import annotations

from collections.abc import Iterable

from . import db, linked

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
        item["shared"] = shared_with(item["account_id"], item["sku"])
        result.append(item)
    return result


def shared_with(account_id: int, set_sku: str) -> list[dict]:
    """Сопоставленные карточки, которые собираются по этому составу.

    Своего набора у них нет — действует этот. Карточка со своим составом в
    список не попадает: её собирают по её собственному.
    """
    source = linked.card(account_id, set_sku)
    return [row for row in linked.others(account_id, set_sku)
            if source_of(row["account_id"], row["sku"]) == source]


def _has_parts(card: linked.Card) -> bool:
    row = db.query_one(
        "SELECT 1 FROM product_sets s "
        "JOIN product_set_items i ON i.account_id = s.account_id AND i.set_sku = s.sku "
        "WHERE s.account_id = ? AND s.sku = ? AND s.active = 1 LIMIT 1",
        card,
    )
    return row is not None


def source_of(account_id: int, sku: str) -> linked.Card | None:
    """Чей состав действует для карточки: свой, а нет своего — сопоставленной.

    Если наборы заданы нескольким карточкам группы, берём основной, дальше — по
    порядку кабинетов. Выключенный или пустой набор не в счёт: такой товар
    собирается как обычный.
    """
    own = linked.card(account_id, sku)
    if _has_parts(own):
        return own
    for row in linked.others(account_id, sku):
        other = linked.card(row["account_id"], row["sku"])
        if _has_parts(other):
            return other
    return None


def set_keys() -> set[tuple[int, str]]:
    """Все карточки, которым задан набор, — (кабинет, SKU). Для значков в каталоге."""
    return {(row["account_id"], row["sku"]) for row in db.query("SELECT account_id, sku FROM product_sets")}


def set_skus(account_id: int) -> set[str]:
    """SKU, у которых есть рабочий состав. Пустой набор набором не считается."""
    rows = db.query(
        "SELECT DISTINCT s.sku FROM product_sets s "
        "JOIN product_set_items i ON i.account_id = s.account_id AND i.set_sku = s.sku "
        "WHERE s.account_id = ? AND s.active = 1",
        (account_id,),
    )
    return {row["sku"] for row in rows}


def parts_for(account_id: int, skus: Iterable[str]) -> dict[str, list[dict]]:
    """Состав для позиций заказа: {SKU этого кабинета: части}.

    Состав — свой или сопоставленной карточки (source_of). Отдаём только
    рабочие наборы: выключенный или пустой набор собирается как обычный товар,
    по своему штрихкоду.
    """
    grouped: dict[str, list[dict]] = {}
    for sku in dict.fromkeys(str(sku) for sku in skus if sku):
        source = source_of(account_id, sku)
        if source is not None:
            grouped[sku] = _parts_view(*source)
    return grouped


def _parts_view(account_id: int, set_sku: str) -> list[dict]:
    """Части набора для сборщика: название и фото — свои или сопоставленной карточки."""
    rows = db.query(
        """
        SELECT i.*, p.name AS product_name, p.image, p.offer_id
        FROM product_set_items i
        LEFT JOIN products p ON p.account_id = i.account_id AND p.sku = i.part_sku
        WHERE i.account_id = ? AND i.set_sku = ?
        ORDER BY i.sort, i.part_key
        """,
        (account_id, set_sku),
    )
    parts = []
    for row in rows:
        part = dict(row)
        if not part.get("image"):
            # Часть штрихкодом — фото у того, чей это штрихкод в каталоге.
            cards = ({linked.card(account_id, part["part_sku"])} if part.get("part_sku")
                     else linked.owners([part.get("barcode") or ""]))
            part["image"] = linked.photo(cards)
        parts.append(part)
    return parts


def parents_of(account_id: int, *, sku: str | None = None, barcodes: list[str] | None = None) -> list[dict]:
    """В какие наборы этого кабинета входит отсканированное. Обычно ровно в один.

    Ищем и по SKU (часть есть в каталоге площадки), и по штрихкоду (части в
    каталоге нет) — с учётом сопоставления: часть узнаётся и по штрихкоду
    сопоставленной с ней карточки, а набор, заданный другой карточке группы,
    действует и здесь. set_sku в ответе — SKU этого кабинета: по нему заказ и
    собирается. Одна часть может входить в несколько наборов — какой из них
    засчитать, решает состав заказа, а не эта функция.
    """
    codes = [str(code) for code in (barcodes or []) if code]
    cards = linked.cards_of_code(codes)
    if sku:
        cards |= linked.group([linked.card(account_id, sku)])
    known = sorted(set(codes) | linked.codes_of(cards))
    conditions: list[str] = []
    params: list = []
    if known:
        conditions.append(f"i.barcode IN ({','.join('?' for _ in known)})")
        params += known
    for owner, part_sku in sorted(cards):
        conditions.append("(i.account_id = ? AND i.part_sku = ?)")
        params += [owner, part_sku]
    if not conditions:
        return []
    rows = db.query(
        f"""
        SELECT i.* FROM product_set_items i
        JOIN product_sets s ON s.account_id = i.account_id AND s.sku = i.set_sku AND s.active = 1
        WHERE {' OR '.join(conditions)}
        ORDER BY i.account_id, i.set_sku, i.sort, i.part_key
        """,
        params,
    )
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        source = linked.card(row["account_id"], row["set_sku"])
        for local in _local_sets(account_id, source):
            if (local, row["part_key"]) in seen:
                continue
            seen.add((local, row["part_key"]))
            found.append({**dict(row), "set_sku": local, "account_id": int(account_id),
                          "source_account_id": row["account_id"], "source_sku": row["set_sku"]})
    return found


def _local_sets(account_id: int, source: linked.Card) -> list[str]:
    """SKU этого кабинета, которые собираются по составу source."""
    account_id = int(account_id)
    return sorted(sku for owner, sku in linked.group([source])
                  if owner == account_id and source_of(owner, sku) == source)


# ------------------------------------------------------------------ прогресс сборки
def part_slot(set_sku: str, part_key: str) -> str:
    """Ключ прогресса по части набора внутри одного заказа."""
    return f"{set_sku}#{part_key}"


def progress(set_sku: str, need: int, direct: int, parts: list[dict], scanned: dict) -> tuple[int, dict]:
    """Сколько наборов собрано и что ещё осталось взять с полки.

    Набор считается собранным, когда набраны все его части: по одной неполной
    части нельзя закрыть позицию, иначе в коробку уедет половина комплекта.
    Отсюда минимум по частям, а не сумма.

    Штрихкод самого набора тоже засчитывается (direct) — если такая наклейка на
    складе есть, сканировать части незачем. Тогда и частей нужно меньше.

    set_sku — ключ позиции в заказе: SKU у Ozon, номер позиции у Маркета,
    единица товара у Avito. По нему считаются части в scanned.
    """
    from_parts = None
    left = max(0, need - direct)
    rows = []
    for part in parts:
        per_set = max(1, int(part.get("quantity") or 1))
        got = int(scanned.get(part_slot(set_sku, part["part_key"]), 0))
        part_need = per_set * left
        from_parts = got // per_set if from_parts is None else min(from_parts, got // per_set)
        rows.append({
            "part_key": part["part_key"],
            "sku": part.get("part_sku"),
            "barcode": part.get("barcode"),
            "name": part_title(part),
            "image": part.get("image"),
            "per_set": per_set,
            "need": part_need,
            "scanned": got,
            "ok": got >= part_need,
        })
    total = direct + min(from_parts or 0, left)
    return total, {"is_set": True, "parts": rows}


def pick_part(items: list[dict], parents: list[dict], set_of) -> tuple[dict, dict] | None:
    """Какой позиции заказа засчитать отсканированную часть и какую именно часть.

    items — позиции после progress(), parents — ответ parents_of(), set_of(item)
    — SKU набора этой позиции в кабинете. Часть может подходить нескольким
    позициям (или одна позиция — несколько раз, у Avito по единицам): берём
    первую, где она ещё нужна. Нужна нигде — первую подходящую, чтобы сказать
    «лишнее». None — часть не из этого заказа.
    """
    pairs = []
    for item in items:
        if not item.get("is_set"):
            continue
        keys = {parent["part_key"] for parent in parents if parent["set_sku"] == set_of(item)}
        pairs += [(item, part) for part in item["parts"] if part["part_key"] in keys]
    if not pairs:
        return None
    return next(((item, part) for item, part in pairs if not item.get("ok") and not part["ok"]), pairs[0])


def missing_parts(item: dict) -> str | None:
    """Недостающие части набора словами — или None, если позиция не набор."""
    if not item.get("is_set"):
        return None
    left = [f"{part['name']} — {part['need'] - part['scanned']} шт" for part in item["parts"] if not part["ok"]]
    return ", ".join(left) or None


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
