"""Сопоставление карточек: один товар склада — несколько карточек площадок.

Один и тот же товар лежит в кабинетах под разными карточками: у Ozon свой SKU,
у Маркета артикул продавца, у Avito свой номер объявления. Для площадки это
разные товары, для склада — одна коробка на полке.

Сопоставление сводит такие карточки в группу. В каталоге группа занимает одну
строку, а не три; по любой карточке видно, где ещё продаётся этот товар.

Панель ничего не связывает сама. Она только показывает совпадения по артикулу
продавца и ждёт подтверждения: одинаковый артикул на двух площадках — частое
совпадение, но не доказательство, и «Гель для стирки 2 л» с «Гель для стирки
детский 2 л» лучше не сводить молча. Ответ «не сопоставлять» — такое же
решение, как «подтвердить»: артикул уходит из предложений и больше не мозолит
глаза.

Отменить сопоставление можно в любой момент: карточки снова становятся
отдельными. Совпадение по артикулу после этого вернётся в предложения — чтобы
оно не возвращалось, рядом стоит «не сопоставлять».
"""
from __future__ import annotations

import uuid

from . import db


def article_of(value: str | None) -> str:
    """Артикул для сравнения: без пробелов по краям и без учёта регистра."""
    return str(value or "").strip().lower()


def _new_group_id() -> str:
    return uuid.uuid4().hex


CARD_SQL = """
SELECT p.account_id, p.sku, p.offer_id, p.name, p.image, p.barcodes, p.archived,
       a.title AS shop, a.marketplace AS market, a.sort AS shop_sort,
       l.group_id, l.is_main
  FROM products p
  JOIN accounts a ON a.id = p.account_id
  LEFT JOIN product_links l ON l.account_id = p.account_id AND l.sku = p.sku
"""


def _card_view(row) -> dict:
    """Карточка для экрана: штрихкоды списком, площадка — по-человечески."""
    from ..markets import registry

    card = dict(row)
    card["barcodes"] = db.json_list(card.get("barcodes"))
    market = registry.get(card.get("market"))
    card["market_title"] = market.title if market else card.get("market")
    card["is_main"] = bool(card.get("is_main"))
    return card


def cards_of(group_id: str) -> list[dict]:
    """Все карточки группы: главная первой, остальные по магазину."""
    rows = db.query(
        CARD_SQL + " WHERE l.group_id = ? ORDER BY l.is_main DESC, a.sort, a.id, p.sku",
        (group_id,),
    )
    return [_card_view(row) for row in rows]


def group_of(account_id: int, sku: str) -> str | None:
    row = db.query_one(
        "SELECT group_id FROM product_links WHERE account_id = ? AND sku = ?",
        (int(account_id), str(sku)),
    )
    return row["group_id"] if row else None


def main_of(account_id: int, sku: str) -> dict | None:
    """Главная карточка товара, к которому относится эта. Нет группы — None."""
    group_id = group_of(account_id, sku)
    if not group_id:
        return None
    for card in cards_of(group_id):
        if card["is_main"]:
            return card
    return None


# ------------------------------------------------------------------ предложения
def _skipped() -> set[str]:
    return {row["article"] for row in db.query("SELECT article FROM product_link_skips")}


def suggestions(account_ids: list[int] | None = None, limit: int = 200) -> list[dict]:
    """Совпадения по артикулу, которые ждут решения человека.

    Берём артикулы, встречающиеся живыми карточками больше чем в одном
    кабинете. Уже сведённые и отвергнутые не показываем: предложение — это
    работа, и список должен пустеть, а не висеть вечно.
    """
    rows = db.query(
        """
        SELECT LOWER(TRIM(p.offer_id)) AS article
          FROM products p
          JOIN accounts a ON a.id = p.account_id
         WHERE p.archived = 0 AND p.offer_id IS NOT NULL AND TRIM(p.offer_id) != ''
         GROUP BY article
        HAVING COUNT(DISTINCT p.account_id) > 1
         ORDER BY article
        """
    )
    skipped = _skipped()
    found: list[dict] = []
    for row in rows:
        article = row["article"]
        if article in skipped:
            continue
        cards = cards_by_article(article)
        if len(cards) < 2:
            continue
        groups = {card["group_id"] for card in cards}
        if len(groups) == 1 and None not in groups:
            continue                       # всё уже сведено — предлагать нечего
        if account_ids and not any(card["account_id"] in account_ids for card in cards):
            continue
        barcodes = [tuple(sorted(card["barcodes"])) for card in cards if card["barcodes"]]
        found.append({
            "article": article,
            "title": next((card["name"] for card in cards if card["name"]), article),
            "cards": cards,
            "shops": len({card["account_id"] for card in cards}),
            # Артикул совпал, а штрихкоды разные — повод посмотреть глазами:
            # чаще всего это разные товары одной линейки.
            "barcodes_differ": len(set(barcodes)) > 1,
        })
        if len(found) >= limit:
            break
    return found


def cards_by_article(article: str) -> list[dict]:
    """Живые карточки всех кабинетов с таким артикулом."""
    rows = db.query(
        CARD_SQL + " WHERE p.archived = 0 AND LOWER(TRIM(p.offer_id)) = ? ORDER BY a.sort, a.id, p.sku",
        (article_of(article),),
    )
    return [_card_view(row) for row in rows]


# ------------------------------------------------------------------ связывание
def _pick_main(cards: list[dict]) -> dict:
    """Кто в группе главный: уже назначенный, иначе карточка со штрихкодом.

    Без штрихкода карточка бесполезна для сборки, а её название и фото пойдут
    в каталог — поэтому главной такую делаем в последнюю очередь.
    """
    for card in cards:
        if card.get("is_main"):
            return card
    with_codes = [card for card in cards if card.get("barcodes")]
    named = [card for card in (with_codes or cards) if card.get("name")]
    return (named or with_codes or cards)[0]


def _write_group(conn, group_id: str, cards: list[dict], main: dict, user: dict | None) -> None:
    now = db.now_iso()
    login = (user or {}).get("login")
    for card in cards:
        conn.execute(
            """
            INSERT INTO product_links(group_id, account_id, sku, is_main, created_at, created_by)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_id, sku) DO UPDATE SET group_id = excluded.group_id,
                is_main = excluded.is_main
            """,
            (group_id, card["account_id"], card["sku"],
             1 if (card["account_id"], card["sku"]) == (main["account_id"], main["sku"]) else 0,
             now, login),
        )


def _join(cards: list[dict], *, user: dict | None = None, main: dict | None = None) -> dict:
    """Свести карточки в одну группу. Уже связанные переносим целиком.

    Если часть карточек состоит в группах, забираем эти группы целиком: иначе
    получилось бы, что товар наполовину сведён с одним кабинетом, наполовину с
    другим, и в каталоге он раздвоится.
    """
    known = {(card["account_id"], str(card["sku"])): card for card in cards}
    for group_id in {card["group_id"] for card in cards if card["group_id"]}:
        for card in cards_of(group_id):
            known.setdefault((card["account_id"], str(card["sku"])), card)
    members = list(known.values())
    if len(members) < 2:
        raise ValueError("Сопоставлять нечего: нужно хотя бы две карточки")
    chosen = main or _pick_main(members)
    group_id = next((card["group_id"] for card in members if card["group_id"]), None) or _new_group_id()
    with db.write() as conn:
        _write_group(conn, group_id, members, chosen, user)
    return {"group_id": group_id, "cards": cards_of(group_id)}


def confirm(article: str, *, user: dict | None = None) -> dict:
    """Подтвердить совпадение по артикулу — свести все его карточки."""
    cards = cards_by_article(article)
    if len(cards) < 2:
        raise ValueError(f"Артикул {article}: сопоставлять не с чем")
    result = _join(cards, user=user)
    db.log_event(
        "products_linked", user=user,
        message=f"Сопоставлено по артикулу {article}: карточек {len(result['cards'])}",
    )
    return result


def confirm_all(account_ids: list[int] | None = None, *, user: dict | None = None) -> int:
    """Подтвердить все предложения разом — когда артикулы ведутся аккуратно."""
    done = 0
    for suggestion in suggestions(account_ids):
        try:
            _join(suggestion["cards"], user=user)
        except ValueError:
            continue
        done += 1
    if done:
        db.log_event("products_linked", user=user, message=f"Подтверждено совпадений: {done}")
    return done


def link(main: tuple[int, str], others: list[tuple[int, str]], *, user: dict | None = None) -> dict:
    """Ручное сопоставление: основной товар и всё, что к нему привязывают."""
    cards = []
    for account_id, sku in [main, *others]:
        card = card_at(account_id, sku)
        if card is None:
            raise ValueError(f"Товара {sku} нет в каталоге кабинета")
        cards.append(card)
    chosen = cards[0]
    result = _join(cards, user=user, main=chosen)
    db.log_event(
        "products_linked", user=user,
        message=f"Сопоставлено вручную: {chosen.get('name') or chosen['sku']}, карточек {len(result['cards'])}",
    )
    return result


def card_at(account_id: int, sku: str) -> dict | None:
    row = db.query_one(
        CARD_SQL + " WHERE p.account_id = ? AND p.sku = ?", (int(account_id), str(sku))
    )
    return _card_view(row) if row else None


def set_main(account_id: int, sku: str, *, user: dict | None = None) -> bool:
    """Сделать карточку главной в её группе."""
    group_id = group_of(account_id, sku)
    if not group_id:
        return False
    with db.write() as conn:
        conn.execute("UPDATE product_links SET is_main = 0 WHERE group_id = ?", (group_id,))
        conn.execute(
            "UPDATE product_links SET is_main = 1 WHERE account_id = ? AND sku = ?",
            (int(account_id), str(sku)),
        )
    db.log_event("products_link_main", user=user, sku=str(sku),
                 message="Изменён основной товар группы")
    return True


# ------------------------------------------------------------------ отмена
def unlink(group_id: str, *, user: dict | None = None) -> int:
    """Отменить сопоставление: карточки снова отдельные товары.

    Совпадение по артикулу после этого вернётся в предложения — это и правильно:
    отмена говорит «сейчас не связаны», а не «никогда не предлагать». Чтобы
    предложение не возвращалось, рядом есть «не сопоставлять».
    """
    cards = cards_of(group_id)
    if not cards:
        return 0
    with db.write() as conn:
        removed = conn.execute(
            "DELETE FROM product_links WHERE group_id = ?", (group_id,)
        ).rowcount or 0
    main = next((card for card in cards if card["is_main"]), cards[0])
    db.log_event(
        "products_unlinked", user=user,
        message=f"Отменено сопоставление: {main.get('name') or main['sku']}, карточек {removed}",
    )
    return removed


def unlink_card(account_id: int, sku: str, *, user: dict | None = None) -> bool:
    """Вынуть одну карточку из группы. Осталась одна — группы больше нет."""
    group_id = group_of(account_id, sku)
    if not group_id:
        return False
    with db.write() as conn:
        conn.execute(
            "DELETE FROM product_links WHERE account_id = ? AND sku = ?",
            (int(account_id), str(sku)),
        )
    left = cards_of(group_id)
    if len(left) < 2:
        with db.write() as conn:
            conn.execute("DELETE FROM product_links WHERE group_id = ?", (group_id,))
    elif not any(card["is_main"] for card in left):
        # Вынули главную — группа без главной карточки бессмысленна.
        chosen = _pick_main(left)
        set_main(chosen["account_id"], chosen["sku"], user=user)
    db.log_event("products_unlinked", user=user, sku=str(sku),
                 message="Карточка вынута из сопоставления")
    return True


def skip(article: str, *, user: dict | None = None) -> None:
    """Больше не предлагать этот артикул."""
    article = article_of(article)
    if not article:
        return
    db.execute(
        "INSERT INTO product_link_skips(article, created_at, created_by) VALUES(?,?,?) "
        "ON CONFLICT(article) DO NOTHING",
        (article, db.now_iso(), (user or {}).get("login")),
    )
    db.log_event("products_link_skipped", user=user, message=f"Не сопоставлять артикул {article}")


def unskip(article: str, *, user: dict | None = None) -> None:
    """Вернуть артикул в предложения."""
    db.execute("DELETE FROM product_link_skips WHERE article = ?", (article_of(article),))
    db.log_event("products_link_skipped", user=user,
                 message=f"Артикул {article} вернули в предложения")


def skipped() -> list[dict]:
    """Отвергнутые артикулы с их карточками — чтобы решение можно было пересмотреть."""
    rows = db.query("SELECT article, created_at, created_by FROM product_link_skips ORDER BY article")
    out = []
    for row in rows:
        cards = cards_by_article(row["article"])
        out.append({**dict(row), "cards": cards,
                    "title": next((card["name"] for card in cards if card["name"]), row["article"])})
    return out


# ------------------------------------------------------------------ чтение
def groups(account_ids: list[int] | None = None, q: str = "", limit: int = 200) -> list[dict]:
    """Сопоставленные товары: группа, её главная карточка и остальные."""
    conditions = ["1 = 1"]
    params: list = []
    if account_ids:
        marks = ",".join("?" for _ in account_ids)
        conditions.append(f"l.group_id IN (SELECT group_id FROM product_links WHERE account_id IN ({marks}))")
        params += list(account_ids)
    if q.strip():
        like = f"%{q.strip()}%"
        conditions.append("(p.name LIKE ? OR p.offer_id LIKE ? OR p.sku LIKE ?)")
        params += [like, like, like]
    rows = db.query(
        f"SELECT DISTINCT l.group_id FROM product_links l "
        f"JOIN products p ON p.account_id = l.account_id AND p.sku = l.sku "
        f"WHERE {' AND '.join(conditions)} LIMIT ?",
        params + [limit],
    )
    out = []
    for row in rows:
        cards = cards_of(row["group_id"])
        if not cards:
            continue
        main = next((card for card in cards if card["is_main"]), cards[0])
        out.append({"group_id": row["group_id"], "main": main, "cards": cards,
                    "title": main.get("name") or main["sku"]})
    out.sort(key=lambda group: (group["title"] or "").lower())
    return out


def stats(account_ids: list[int] | None = None) -> dict[str, int]:
    """Счётчики для вкладок: сопоставлено, ждёт решения, отвергнуто."""
    linked = db.query_one("SELECT COUNT(DISTINCT group_id) AS c FROM product_links")["c"]
    return {
        "groups": linked,
        "suggestions": len(suggestions(account_ids)),
        "skipped": db.query_one("SELECT COUNT(*) AS c FROM product_link_skips")["c"],
    }
