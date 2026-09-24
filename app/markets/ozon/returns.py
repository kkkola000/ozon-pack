"""Возвраты Ozon целиком: таблица, разбор, загрузка, статусы, акт из «полученных».

Единственная площадка, у которой возврат — отдельная сущность со своим
методом API (/v1/returns/list) и своими статусами. Настройки «какие статусы
считать готовыми к выдаче / полученными» тоже здесь: это статусы Ozon, и ядру
они ни к чему.
"""
from __future__ import annotations

import logging
import sqlite3

from ...core import db, return_acts
from ...core import sync as core_sync
from ...core.config import settings
from ...core.return_acts import NO_SHEET
from ...core.returns_pdf import barcode_svg, cut, mark_cell
from ...core.store import _DAY, _dt, _moment, _raw_json, _text, _with_mark, local_day, local_time
from ..base import ReturnsSource
from . import client as ozon
from .client import OzonError, iso_moment

log = logging.getLogger("ozon.returns")

SCHEMA = """
CREATE TABLE IF NOT EXISTS returns (
    account_id        INTEGER NOT NULL,
    id                TEXT NOT NULL,
    type              TEXT,
    scheme            TEXT,
    status_sys        TEXT,
    status_name       TEXT,
    order_id          INTEGER,
    order_number      TEXT,
    posting_number    TEXT,
    sku               TEXT,
    offer_id          TEXT,
    product_name      TEXT,
    quantity          INTEGER DEFAULT 1,
    price             TEXT,
    currency          TEXT,
    place_name        TEXT,
    place_address     TEXT,
    target_place_name TEXT,
    return_reason     TEXT,
    return_date       TEXT,
    final_moment      TEXT,
    -- visual.change_moment: когда площадка в последний раз меняла статус. По
    -- нему же строится окно загрузки, а для акта «без статуса» это
    -- единственное известное число — получения у такого акта ещё нет.
    status_changed_at TEXT,
    -- storage.arrived_moment: когда возврат стал готов к выдаче, то есть
    -- доехал до пункта. Видно в списке «К выдаче»: по нему понятно, что лежит
    -- давно, а что привезли только что.
    arrived_at        TEXT,
    storage_until     TEXT,
    storage_sum       TEXT,
    barcode           TEXT,
    is_ready          INTEGER NOT NULL DEFAULT 0,
    raw               TEXT,
    printed_at        TEXT,
    -- Отметка сборщика: возврат приняли ('ok') или не приняли ('bad'), плюс
    -- комментарий. Всё это заводит панель, площадка о таких отметках не знает,
    -- поэтому синхронизация эти колонки не трогает.
    mark              TEXT,
    note              TEXT,
    mark_at           TEXT,
    mark_by           TEXT,
    -- Акт, по которому за возвратом ездили. Ставится один раз и больше не
    -- меняется: это же и защита от повторной загрузки — возврат, уже попавший
    -- в акт, во второй акт не возьмут, даже когда акт подтверждён и закрыт.
    act_id            TEXT,
    -- Когда панель впервые увидела возврат в статусе «Получен». Пишется один
    -- раз: Ozon отдаёт этот статус и дальше, а повторная запись означала бы
    -- второй акт на тот же возврат.
    received_at       TEXT,
    -- Тот же момент местной датой — по ней возвраты грузят «за указанное
    -- число». Считать дату из received_at в запросе нельзя: сдвиг часового
    -- пояса живёт в настройках, а не в SQLite.
    received_day      TEXT,
    first_seen_at     TEXT,
    updated_at        TEXT,
    PRIMARY KEY (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_returns_ready ON returns(account_id, is_ready, type);
CREATE INDEX IF NOT EXISTS idx_returns_act ON returns(act_id);
CREATE INDEX IF NOT EXISTS idx_returns_received ON returns(account_id, received_day, act_id);

-- Акт получения возвратов: возвраты, которые перешли в статус «Получен», —
-- одна поездка в пункт выдачи. Акт закрывает поездку целиком: и FBS, и FBO.
-- Акт бывает и по всем кабинетам сразу, поэтому к кабинету не привязан жёстко:
-- строки внутри могут быть из разных кабинетов и с разных площадок.
"""


# Возврат «готов к выдаче» — статусы из /v1/returns/list (visual.status.sys_name).
RETURN_STATUS_LABELS = {
    "ArrivedAtReturnPlace": "В пункте выдачи",
    "MovingToSeller": "Едет к продавцу",
    "WaitingShipment": "Ожидает отгрузки",
    "ReturningByCourier": "Везёт курьер",
    "ReceivedBySeller": "Получен продавцом",
    "MovingToOzon": "Едет на склад Ozon",
    "ReturnedToOzon": "На складе Ozon",
    "Utilizing": "На утилизации",
    "Utilized": "Утилизирован",
    "Cancelled": "Отменён",
}


def upsert_return(conn: sqlite3.Connection, account_id: int, raw: dict) -> str:
    """Возврат из /v1/returns/list (единый метод для FBO и FBS)."""
    return_id = str(raw.get("id") or "")
    if not return_id:
        raise ValueError("В ответе Ozon нет id возврата")

    product = raw.get("product") or {}
    place = raw.get("place") or {}
    target = raw.get("target_place") or {}
    storage = raw.get("storage") or {}
    logistic = raw.get("logistic") or {}
    visual = raw.get("visual") or {}
    status = visual.get("status") or {}
    price = product.get("price") or {}


    sys_name = _text(status.get("sys_name")) or ""
    # Готов к выдаче ровно тогда, когда Ozon сообщает нужный статус
    # (по умолчанию ArrivedAtReturnPlace — «В пункте выдачи»).
    is_ready = 1 if sys_name in set(get_returns_statuses()) else 0
    # «Получен» — возврат уже у нас, и по нему нужна отметка. Один и тот же
    # статус в обоих списках означает «ещё к выдаче»: сборщик за ним едет, а
    # класть в акт то, что не забрали, нельзя.
    received = not is_ready and sys_name in set(get_received_statuses())

    now = db.now_iso()
    # Когда возврат получили — берём у площадки, а не ставим «сейчас». Ozon
    # отдаёт по статусу «Получен» весь архив, и с временем «сейчас» первое же
    # обновление объявило бы полученными сегодня тысячи старых возвратов —
    # ровно один такой акт на 2446 позиций и получился в версии 1.17.
    #
    # Число — по final_moment, «прибыл на фулфилмент или выдан продавцу»: это
    # сам момент получения. change_moment запасной, это лишь последняя смена
    # статуса: она бывает раньше выдачи — статус перещёлкнулся 18-го, а на
    # руки возврат отдали 19-го, и в акт он вставал не тем числом.
    platform_moment = (
        _moment(logistic.get("final_moment")) or _moment(visual.get("change_moment"))
    ) if received else None
    if platform_moment and platform_moment > now:
        platform_moment = now
    # «Сейчас» — только если у площадки внятного момента нет вовсе.
    received_at = platform_moment or (now if received else None)
    # Число обязано быть числом: по нему и только по нему возврат попадает в
    # акт. Если из момента его не вышло, ставим сегодняшнее — возврат лучше
    # положить в акт не за тот день, чем потерять с экрана совсем.
    received_day = local_day(received_at) if received_at else None
    if received_at and not _DAY.fullmatch(received_day or ""):
        received_day = local_day(now)

    existing = conn.execute(
        "SELECT first_seen_at FROM returns WHERE account_id = ? AND id = ?", (account_id, return_id)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO returns (
            account_id, id, type, scheme, status_sys, status_name, order_id, order_number, posting_number, sku, offer_id,
            product_name, quantity, price, currency, place_name, place_address, target_place_name, return_reason,
            return_date, final_moment, status_changed_at, arrived_at, storage_until, storage_sum, barcode,
            is_ready, raw, first_seen_at, updated_at, received_at, received_day
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, id) DO UPDATE SET
            type = excluded.type, scheme = excluded.scheme, status_sys = excluded.status_sys,
            status_name = excluded.status_name, order_id = excluded.order_id, order_number = excluded.order_number,
            posting_number = excluded.posting_number, sku = excluded.sku, offer_id = excluded.offer_id,
            product_name = excluded.product_name, quantity = excluded.quantity, price = excluded.price,
            currency = excluded.currency, place_name = excluded.place_name, place_address = excluded.place_address,
            target_place_name = excluded.target_place_name, return_reason = excluded.return_reason,
            return_date = excluded.return_date, final_moment = excluded.final_moment,
            status_changed_at = excluded.status_changed_at, arrived_at = excluded.arrived_at,
            storage_until = excluded.storage_until, storage_sum = excluded.storage_sum, barcode = excluded.barcode,
            is_ready = excluded.is_ready, raw = excluded.raw, updated_at = excluded.updated_at,
            -- Здесь момент получения только дописывается, но не перебивается:
            -- в excluded он может оказаться «сейчас», а Ozon отдаёт статус
            -- «Получен» и на следующих обновлениях — каждое двигало бы дату
            -- вперёд. Настоящий момент площадки ставит отдельный UPDATE ниже.
            received_at = COALESCE(returns.received_at, excluded.received_at),
            received_day = COALESCE(returns.received_day, excluded.received_day)
        """,
        (
            account_id,
            return_id,
            _text(raw.get("type")),
            _text(raw.get("schema")),
            sys_name or None,
            _text(status.get("display_name")) or RETURN_STATUS_LABELS.get(sys_name, sys_name),
            raw.get("order_id"),
            _text(raw.get("order_number")),
            _text(raw.get("posting_number")),
            _text(product.get("sku")),
            _text(product.get("offer_id")),
            _text(product.get("name")),
            int(product.get("quantity") or 1),
            _text(price.get("price")),
            _text(price.get("currency_code")),
            _text(place.get("name")),
            _text(place.get("address")),
            _text(target.get("name")),
            _text(raw.get("return_reason_name")),
            _dt(logistic.get("return_date")),
            _dt(logistic.get("final_moment")),
            _moment(visual.get("change_moment")),
            _moment(storage.get("arrived_moment")),
            _dt(storage.get("utilization_forecast_date")),
            _text((storage.get("sum") or {}).get("price")),
            _text(logistic.get("barcode")),
            is_ready,
            _raw_json(raw),
            (existing["first_seen_at"] if existing else now) or now,
            now,
            received_at,
            received_day,
        ),
    )
    # Момент от площадки — величина постоянная, и если он приехал позже или
    # изменился, число надо поправить. Без этого первая запись залипала: статус
    # сменился 18-го, выдали 19-го, а возврат так и оставался за 18-м, сколько
    # ни обновляй. «Сейчас» сюда не попадает — им затирать нечего.
    #
    # Возврат из подтверждённого акта не трогаем: там работа закрыта, и менять
    # под ней число значило бы переписывать уже подписанный документ.
    if platform_moment:
        conn.execute(
            "UPDATE returns SET received_at = ?, received_day = ? "
            "WHERE account_id = ? AND id = ? AND (act_id IS NULL OR act_id IN "
            "(SELECT id FROM return_acts WHERE confirmed_at IS NULL))",
            (platform_moment, local_day(platform_moment), account_id, return_id),
        )
        # Число переехало, а возврат остался в акте за прежнее — и «Обновить»
        # не помогало: в акте за 18-е так и висел возврат, полученный 19-го.
        # Освобождаем, чтобы он попал в акт за своё число. Акт при этом не
        # составляется: его по-прежнему заводит человек кнопкой.
        #
        # Отмеченный возврат остаётся на месте: отметку ставил сборщик, держа
        # возврат в руках, и переносить её работу в другой акт нельзя.
        conn.execute(
            "UPDATE returns SET act_id = NULL WHERE account_id = ? AND id = ? "
            "AND mark IS NULL AND act_id IN (SELECT id FROM return_acts "
            "WHERE confirmed_at IS NULL AND received_day IS NOT NULL "
            "AND received_day <> ?)",
            (account_id, return_id, local_day(platform_moment)),
        )
    return return_id


def return_view(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data.pop("raw", None)
    data["status_label"] = data.get("status_name") or RETURN_STATUS_LABELS.get(data.get("status_sys") or "", "")
    # Когда возврат стал готов к выдаче. Только показываем: по нему видно, что
    # лежит в пункте давно, а что привезли сегодня. Список по нему не строится
    # и не сортируется — состав раздела решают отмеченные статусы.
    data["arrived_local"] = local_time(data.get("arrived_at"), "%d.%m.%Y") if data.get("arrived_at") else ""
    return _with_mark(data)


def _rebuild_pickup(account_id: int, pickup: list[str], seen: set[str]) -> int:
    """Пересобрать «К выдаче» по тому, что Ozon только что отдал.

    Раздел строится прямо по статусу строки, а статус панель знает только из
    загрузки. Возврат, уехавший из пункта, приходит уже в чужом статусе
    (MovingToSeller) — а его панель не запрашивает вовсе, поэтому в базе
    навсегда оставался прежний «В пункте выдачи», и строка висела в разделе,
    сколько ни жми «Обновить». Сверять было не с чем: признак is_ready раздел
    больше не читает.

    Поэтому то, чего в ответе площадки не оказалось, из выдачи убирается:
    список — снимок последней загрузки, а не то, что когда-то в него попало.
    Только после полного обхода: на оборванном списке это вычистило бы всё,
    до чего не дочитали.

    Возвращает, сколько возвратов ушло из раздела.
    """
    if not pickup:
        return 0
    places = ",".join("?" for _ in pickup)
    left = [
        row["id"]
        for row in db.query(
            f"SELECT id FROM returns WHERE account_id = ? AND status_sys IN ({places})",
            [account_id] + pickup,
        )
        if row["id"] not in seen
    ]
    if not left:
        return 0
    now = db.now_iso()
    # Список рубим на части: первое обновление после этой правки разбирает всё,
    # что накопилось, а SQLite держит ограниченное число параметров в запросе.
    for start in range(0, len(left), 400):
        batch = left[start : start + 400]
        marks = ",".join("?" for _ in batch)
        # Строка без работы — это просто кэш списка выдачи, держать её незачем.
        db.execute(
            f"DELETE FROM returns WHERE account_id = ? AND id IN ({marks}) "
            "AND mark IS NULL AND note IS NULL AND act_id IS NULL",
            [account_id] + batch,
        )
        # А по этим отметка уже стоит или они лежат в акте — их удалять нельзя,
        # сотрём работу сборщика. Статус снимаем: какой он теперь, площадка не
        # сказала, а прежний — заведомо неправда, и по нему строка осталась бы
        # в разделе. Пустой статус покажется прочерком, и это честно.
        db.execute(
            f"UPDATE returns SET is_ready = 0, status_sys = NULL, status_name = NULL, updated_at = ? "
            f"WHERE account_id = ? AND id IN ({marks})",
            [now, account_id] + batch,
        )
    return len(left)


def sync_returns(account: dict | None = None, *, full: bool = False,
                 statuses: list[str] | None = None) -> dict:
    """Возвраты FBO и FBS: /v1/returns/list.

    Забираем два набора статусов. Первый — в которых возврат можно получить
    (по умолчанию ArrivedAtReturnPlace — «В пункте выдачи»): это список к
    поездке. Второй — в которых он уже получен (ReceivedBySeller): по такому
    нужна отметка, и панель сводит такие возвраты в акт на подтверждение.

    Фильтр уходит в запрос, но на него не полагаемся: всё, что пришло с другим
    статусом, отбрасывается на нашей стороне. Иначе достаточно одной перемены
    в API, чтобы сборщик снова увидел лишнее.
    """

    account = core_sync._account(account)
    if account is None:
        return {"returns": 0}
    account_id = account["id"]
    client = ozon.get_client(account)
    wanted = list(statuses or wanted_statuses())
    wanted_set = set(wanted)
    # «К выдаче» забираем по статусу и целиком: возврат лежит в пункте неделями,
    # и окно вымело бы из списка всё, за чем ещё не съездили. Полученные — окном
    # за последние дни: по их статусу Ozon отдаёт весь архив.
    pickup = [status for status in wanted if status in set(get_returns_statuses())]
    received = [status for status in wanted if status not in set(pickup)]
    received_days = get_received_days()
    saved = 0
    skipped = 0
    seen: set[str] = set()
    complete = True
    # Полнота обхода «к выдаче» считается отдельно: по ней список выдачи
    # пересобирается заново, и сбой на выборке полученных не должен этому
    # мешать — это разные запросы к разным статусам.
    pickup_complete = True

    def remember(raw: dict) -> None:
        """Считаем, сколько Ozon вернул не того, что мы просили, — это видно в итоге."""
        nonlocal skipped
        sys_name = (((raw.get("visual") or {}).get("status") or {}).get("sys_name")) or "—"
        if sys_name not in wanted_set:
            skipped += 1

    def store_page(returns: list[dict]) -> int:
        nonlocal saved
        keep = []
        for raw in returns:
            remember(raw)
            sys_name = (((raw.get("visual") or {}).get("status") or {}).get("sys_name")) or ""
            if sys_name in wanted_set:
                keep.append(raw)
        if keep:
            with db.write() as conn:
                for raw in keep:
                    seen.add(upsert_return(conn, account_id, raw))
                    saved += 1
        return len(keep)

    if full:
        # Полный обход без фильтра — чтобы увидеть, что вообще есть в Ozon.
        last_id = 0
        for _page in range(core_sync.RETURNS_MAX_PAGES):
            try:
                returns, has_next = client.returns_list(limit=core_sync.RETURNS_PAGE_LIMIT, last_id=last_id)
            except OzonError as exc:
                log.warning("Возвраты недоступны: %s", exc)
                complete = False
                break
            if not returns:
                break
            store_page(returns)
            last_id = returns[-1].get("id") or 0
            if not has_next or not last_id:
                break
        pickup_complete = complete
    else:
        def walk(filter_: dict, what: str) -> bool:
            """Пролистать выдачу под фильтром. False — обход вышел неполным.

            Флаг полноты не трогаем: решает вызывающий. Неполный обход нельзя
            принимать за полный — по нему панель решает, какие возвраты пропали
            из выдачи, и оборванный список объявил бы пропавшим всё, до чего не
            дочитали. Но у выборки полученных есть запасной путь, и там одна
            неудача ещё не делает весь обход неполным.
            """
            last_id = 0
            for _page in range(core_sync.RETURNS_MAX_PAGES):
                try:
                    # В фильтре /v1/returns/list допускается только одно поле,
                    # поэтому и статусы, и окно запрашиваем по очереди.
                    returns, has_next = client.returns_list(
                        limit=core_sync.RETURNS_PAGE_LIMIT, last_id=last_id, filter_=filter_
                    )
                except OzonError as exc:
                    log.warning("Возвраты (%s) недоступны: %s", what, exc)
                    return False
                if not returns:
                    return True
                store_page(returns)
                last_id = returns[-1].get("id") or 0
                if not has_next or not last_id:
                    return True
            log.warning(
                "Возвраты (%s): упёрлись в потолок %d страниц, список прочитан не до конца",
                what, core_sync.RETURNS_MAX_PAGES,
            )
            return False

        for status in pickup:
            pickup_complete = walk({"visual_status_name": status}, status) and pickup_complete
        complete = pickup_complete and complete

        # Полученные — статус вместе с окном, одним запросом. Фильтра по самому
        # моменту получения (final_moment) в API нет, поэтому окно задаём по
        # смене статуса: она бывает позже получения, значит окно с запасом.
        #
        # Если площадка откажется принимать два поля разом, повторяем с одним
        # окном: лишние статусы отсеет store_page. Терять из-за этого весь обход
        # нельзя — без полученных возвратов не составить ни одного акта.
        if received:
            # Сутки вперёд — запас на расхождение часов: момент, пришедший от
            # площадки на минуту «в будущем», иначе выпал бы из окна.
            since, until = core_sync._iso_window(received_days, 1)
            what = f"получены за {received_days} дн."
            window = {"time_from": iso_moment(since), "time_to": iso_moment(until)}
            # all() с генератором: отказало на первом статусе — остальные
            # откажут так же, и добивать их запросами незачем.
            by_status = all(
                walk({"visual_status_name": status, "visual_status_change_moment": window},
                     f"{status}, {what}")
                for status in received
            )
            if not by_status:
                log.info("Повторяем выборку полученных одним окном, без статуса")
                by_status = walk({"visual_status_change_moment": window}, what)
            complete = by_status and complete

    # Признак «к выдаче» приводим к текущему статусу строки — без оглядки на
    # то, дочитался ли обход. Тут нет догадок: статус взят из самой строки.
    # Иначе возврат, у которого статус давно сменился, остаётся в списке к
    # выдаче и уходит на печать — сборщик едет за тем, чего в пункте нет.
    pickup_places = ",".join("?" for _ in pickup) or "''"
    db.execute(
        "UPDATE returns SET is_ready = 0 WHERE account_id = ? AND is_ready = 1 "
        f"AND (status_sys IS NULL OR status_sys NOT IN ({pickup_places}))",
        [account_id] + pickup,
    )

    gone = _rebuild_pickup(account_id, pickup, seen) if pickup_complete else 0
    removed = 0
    if complete:
        # Записи в ненужных статусах, оставшиеся от прошлых версий или прошлых
        # настроек, убираем совсем. Кроме тех, по которым уже есть работа:
        # отметка, комментарий или акт — удаление стёрло бы результат проверки
        # возврата вместе со строкой. Чистим до раздачи актов: мусор из чужого
        # статуса никто не получал, и заводить на него акт незачем.
        wanted_places = ",".join("?" for _ in wanted) or "''"
        removed = db.execute(
            "DELETE FROM returns WHERE account_id = ? "
            f"AND (status_sys IS NULL OR status_sys NOT IN ({wanted_places})) "
            "AND mark IS NULL AND note IS NULL AND act_id IS NULL",
            [account_id] + wanted,
        ).rowcount or 0

        # Акт собирается только из полученных — обновление в него ничего не
        # кладёт. Раньше возврат, ушедший из выдачи без статуса «Получен»,
        # сметался в акт «без статуса», чтобы не исчезнуть с экрана молча. Но
        # так в акт приёмки попадал и возврат, который ещё едет к продавцу:
        # удалишь акт — обновление положит его обратно. Принимать то, чего нет
        # на складе, нельзя, поэтому акты заводит только человек кнопкой.

    # Возврат мог уйти из акта, если площадка передвинула число получения:
    # акт за 18-е, а получен он 19-го. Акт, из которого так забрали всё,
    # остаётся пустой строкой на экране — убираем.
    return_acts.drop_empty(account_id)

    result = {"returns": saved}
    if skipped:
        result["returns_skipped"] = skipped
    if gone:
        result["returns_gone"] = gone
    if removed:
        result["returns_removed"] = removed
    return result


KV_RETURNS_STATUSES = "returns_ready_statuses"


# Статусы, в которых возврат считается полученным: он уже у нас, и по нему надо
# принять решение. Это отдельная настройка, а не часть списка «готов к выдаче»:
# один список решал бы сразу две задачи — что показывать сборщику к поездке и
# что закрывать актом, — и включить второе без первого было бы нельзя.
KV_RETURNS_RECEIVED = "returns_received_statuses"


# За сколько дней назад забирать полученные возвраты. По статусу «Получен» Ozon
# отдаёт весь архив, от старых к новым, — а читать его можно только страницами.
# У склада с историей архив перерастает любой потолок страниц, и обрезаются как
# раз самые свежие: возврат получен вчера, а панель о нём не знает. Поэтому
# полученные берём окном, а не целиком.
KV_RECEIVED_DAYS = "returns_received_days"


DEFAULT_RECEIVED_DAYS = 7


MAX_RECEIVED_DAYS = 365


RETURN_STATUS_CHOICES = [
    ("ArrivedAtReturnPlace", "В пункте выдачи", "возврат лежит в пункте — его можно забрать"),
    ("WaitingShipment", "Ожидает отгрузки", "готовится к отправке"),
    ("MovingToSeller", "Едет к продавцу", "в пути, забрать нельзя"),
    ("ReturningByCourier", "Везёт курьер", "в пути, забрать нельзя"),
    ("ReceivedBySeller", "Получен продавцом", "уже у вас"),
    ("MovingToOzon", "Едет на склад Ozon", "уезжает на склад Ozon"),
    ("ReturnedToOzon", "На складе Ozon", "хранится у Ozon"),
]


DEFAULT_RETURNS_STATUSES = ["ArrivedAtReturnPlace"]


DEFAULT_RECEIVED_STATUSES = ["ReceivedBySeller"]


# Значения, которые писал в .env установщик прежних версий. Это не осознанный
# выбор пользователя, а устаревшая настройка по умолчанию: файл при обновлении
# не перезаписывается, поэтому старое значение переопределяем на актуальное.
LEGACY_DEFAULTS = {
    ("ArrivedAtReturnPlace", "WaitingShipment"),
    ("ReturnedToSeller", "ReadyForShipment", "WaitingForSeller", "ready_for_shipment", "returned_to_seller"),
}


def get_returns_statuses() -> list[str]:
    raw = (db.kv_get(KV_RETURNS_STATUSES) or "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    from_env = list(settings.returns_ready_statuses)
    if from_env and tuple(from_env) not in LEGACY_DEFAULTS:
        return from_env
    return list(DEFAULT_RETURNS_STATUSES)


def set_returns_statuses(statuses: list[str], user: dict | None = None) -> list[str]:
    cleaned = [s.strip() for s in statuses if s and s.strip()]
    if not cleaned:
        cleaned = list(DEFAULT_RETURNS_STATUSES)
    db.kv_set(KV_RETURNS_STATUSES, ",".join(cleaned))
    db.log_event("returns_statuses_set", user=user, message=", ".join(cleaned))
    return cleaned


def returns_source() -> str:
    return "panel" if (db.kv_get(KV_RETURNS_STATUSES) or "").strip() else "env"


def get_received_statuses() -> list[str]:
    """Статусы, в которых возврат считается полученным.

    Пустое значение — осознанный выбор «акты не вести», поэтому отличаем
    «не задано» (берём умолчание) от «задано пустым».
    """
    raw = db.kv_get(KV_RETURNS_RECEIVED)
    if raw is None:
        return list(DEFAULT_RECEIVED_STATUSES)
    return [item.strip() for item in raw.split(",") if item.strip()]


def set_received_statuses(statuses: list[str], user: dict | None = None) -> list[str]:
    cleaned = [s.strip() for s in statuses if s and s.strip()]
    db.kv_set(KV_RETURNS_RECEIVED, ",".join(cleaned))
    db.log_event("returns_received_set", user=user, message=", ".join(cleaned) or "выключено")
    return cleaned


def get_received_days() -> int:
    """За сколько дней назад забирать полученные возвраты."""
    raw = (db.kv_get(KV_RECEIVED_DAYS) or "").strip()
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RECEIVED_DAYS
    return min(max(days, 1), MAX_RECEIVED_DAYS)


def set_received_days(days: int, user: dict | None = None) -> int:
    """Окно меньше суток и больше года бессмысленно — поэтому и обрезаем."""
    value = min(max(int(days), 1), MAX_RECEIVED_DAYS)
    db.kv_set(KV_RECEIVED_DAYS, str(value))
    db.log_event("returns_received_days_set", user=user, message=f"{value} дн.")
    return value


def wanted_statuses() -> list[str]:
    """Что вообще забирать из Ozon: и к выдаче, и полученное.

    Полученные возвраты надо не только загрузить, но и удержать в базе: чистка
    удаляет записи в незапрошенных статусах, и без этого списка возврат исчез
    бы ровно в тот момент, когда по нему нужно поставить отметку.
    """
    seen: list[str] = []
    for code in list(get_returns_statuses()) + list(get_received_statuses()):
        if code not in seen:
            seen.append(code)
    return seen


def pickup_sql(column: str = "status_sys") -> tuple[str, list[str]]:
    """Условие «этот возврат к выдаче» — прямо по отмеченным статусам.

    Без промежуточного признака: он ставится при загрузке и успевает
    устареть, а раздел «К выдаче» и лист на печать должны отвечать настройке
    здесь и сейчас. Отмечено «В пункте выдачи» — значит только он и есть.
    """
    statuses = list(get_returns_statuses())
    marks = ",".join("?" for _ in statuses) or "''"
    return f"{column} IN ({marks})", statuses


def status_label(sys_name: str) -> str:
    for code, label, _hint in RETURN_STATUS_CHOICES:
        if code == sys_name:
            return label
    return sys_name


# --------------------------------------------------------------- сбор актов
def _free_received(account_id: int) -> tuple[str, list]:
    """Условие «полученный возврат, который ещё можно забрать в акт».

    Одно и на предпросмотр, и на саму сборку: иначе кнопка обещала бы одно, а
    акт собирал бы другое.

    Статус спрашиваем **текущий**, а не только момент получения. Момент
    пишется один раз и не снимается — он про то, что возврат когда-то был
    получен. Возврат, вернувшийся в пункт выдачи, по одному лишь моменту
    попадал в акт приёмки со статусом «В пункте выдачи», хотя на складе его
    нет.
    """

    statuses = list(get_received_statuses())
    marks = ",".join("?" for _ in statuses) or "''"
    sql = (
        f"account_id = ? AND received_at IS NOT NULL AND status_sys IN ({marks}) "
        "AND (act_id IS NULL OR act_id IN (SELECT id FROM return_acts "
        "WHERE kind = ? AND confirmed_at IS NULL))"
    )
    return sql, [account_id] + statuses + [NO_SHEET]


def received_returns(account_id: int, day: str) -> list[str]:
    """Полученные возвраты кабинета за число, которые ещё ни в один акт не попали.

    Это и есть защита от задвоения: возврат с act_id сюда не попадает ни при
    каком повторе — ни когда Ozon снова отдаёт его «полученным», ни когда то же
    число выбрали второй раз.

    Исключение — акт «без статуса»: туда возврат попал по догадке о пропаже, и
    пришедший «Получен» эту догадку заменяет. Иначе на один возврат оказалось
    бы два акта, то есть две отметки на одну работу.

    Число — день перехода в «Получен»: акт это поездка за конкретный день.
    """
    where, params = _free_received(account_id)
    return [
        row["id"] for row in db.query(
            f"SELECT id FROM returns WHERE {where} AND received_day = ? ORDER BY received_at, id",
            params + [day],
        )
    ]


def claim(conn, act_id: str, account_id: int, return_ids: list[str]) -> int:
    """Забрать возвраты в акт. Возвращает, сколько реально переехало.

    Берём свободные и те, что лежат в неподтверждённом акте «без статуса»:
    факт получения от площадки точнее нашей догадки о пропаже. Возврат из
    любого другого акта не трогаем — это и есть защита от второго акта на ту
    же работу.
    """
    placeholders = ",".join("?" for _ in return_ids)
    cursor = conn.execute(
        f"""
        UPDATE returns SET act_id = ?
        WHERE account_id = ? AND id IN ({placeholders})
          AND (act_id IS NULL
               OR act_id IN (SELECT id FROM return_acts
                             WHERE kind = ? AND confirmed_at IS NULL))
        """,
        [act_id, account_id] + list(return_ids) + [NO_SHEET],
    )
    return cursor.rowcount or 0


def from_received(account_id: int, day: str, *, user: dict | None = None) -> dict:
    """Свести в акт полученные возвраты Ozon за число. Сама сборка — в ядре."""
    return return_acts.from_received(SOURCE, account_id, day, user=user)


# ------------------------------------------------------- раздел «Возвраты»
def ready(account_ids: list[int], params: dict | None = None, limit: int = 1000) -> list[dict]:
    """Возвраты, готовые к выдаче, по одному кабинету или сразу по нескольким.

    В панели только то, что лежит в пункте выдачи: забранное Ozon переводит
    дальше сам, и синхронизация убирает такие записи.
    """
    if not account_ids:
        return []
    params = params or {}
    scheme = str(params.get("scheme") or "all")
    place = str(params.get("place") or "")
    q = str(params.get("q") or "")

    placeholders = ",".join("?" for _ in account_ids)
    ready_sql, ready_params = pickup_sql("r.status_sys")
    conditions = [f"r.account_id IN ({placeholders})", ready_sql]
    args: list = list(account_ids) + list(ready_params)
    if scheme in ("FBO", "FBS"):
        conditions.append("(r.type = ? OR r.scheme = ?)")
        args += [scheme, scheme]
    if place:
        conditions.append("r.place_name = ?")
        args.append(place)
    if q:
        like = f"%{q.strip()}%"
        conditions.append(
            "(r.product_name LIKE ? OR r.offer_id LIKE ? OR r.sku LIKE ? OR r.order_number LIKE ?"
            " OR r.posting_number LIKE ? OR r.barcode LIKE ? OR r.id LIKE ?)"
        )
        args += [like] * 7
    where = " WHERE " + " AND ".join(conditions)
    # Пункт выдачи впереди: за возвратами едут в конкретный ПВЗ, и на листе по
    # нескольким кабинетам строки одного пункта должны идти подряд.
    rows = db.query(
        f"SELECT r.*, a.title AS account_title FROM returns r "
        f"LEFT JOIN accounts a ON a.id = r.account_id{where} "
        f"ORDER BY (r.place_name IS NULL), r.place_name, a.title, r.product_name LIMIT ?",
        args + [limit],
    )
    return [return_view(row) for row in rows]


def count_ready(account_ids: list[int]) -> int:
    if not account_ids:
        return 0
    placeholders = ",".join("?" for _ in account_ids)
    ready_sql, ready_params = pickup_sql()
    return db.query_one(
        f"SELECT COUNT(*) AS c FROM returns WHERE {ready_sql} AND account_id IN ({placeholders})",
        list(ready_params) + list(account_ids),
    )["c"]


def places(account_id: int) -> list[str]:
    """Пункты выдачи, в которых что-то лежит, — для фильтра раздела."""
    ready_sql, ready_params = pickup_sql()
    rows = db.query(
        f"SELECT DISTINCT place_name FROM returns WHERE account_id = ? AND {ready_sql} "
        "AND place_name IS NOT NULL ORDER BY place_name",
        [account_id] + list(ready_params),
    )
    return [row["place_name"] for row in rows]


def page(account: dict, params: dict) -> dict:
    """Контекст вкладки «К выдаче» для кабинета Ozon."""
    scheme = str(params.get("scheme") or "all")
    place = str(params.get("place") or "")
    q = str(params.get("q") or "")
    items = ready([account["id"]], params={"scheme": scheme, "place": place, "q": q})

    ready_sql, ready_params = pickup_sql()
    args = [account["id"]] + list(ready_params)
    totals = {
        "ready": db.query_one(
            f"SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND {ready_sql}", args
        )["c"],
        "fbo": db.query_one(
            f"SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND {ready_sql} "
            "AND (type = 'FBO' OR scheme = 'FBO')", args
        )["c"],
        "fbs": db.query_one(
            f"SELECT COUNT(*) AS c FROM returns WHERE account_id = ? AND {ready_sql} "
            "AND (type = 'FBS' OR scheme = 'FBS')", args
        )["c"],
    }
    # Заголовок листа печати: по нему на бумаге видно, с каким фильтром его собрали.
    subtitle = ""
    if scheme != "all":
        subtitle += f"({scheme})"
    if place:
        subtitle += f" · {place}"
    return {
        "items": items,
        "stats": [("Готовы к выдаче", totals["ready"]), ("FBO", totals["fbo"]), ("FBS", totals["fbs"])],
        "totals": totals,
        "places": places(account["id"]),
        "scheme": scheme,
        "place": place,
        "q": q,
        "sheet_subtitle": subtitle.strip(),
    }


def act_rows(act_id: str) -> list[dict]:
    rows = db.query(
        "SELECT r.*, a.title AS account_title FROM returns r "
        "LEFT JOIN accounts a ON a.id = r.account_id WHERE r.act_id = ? "
        "ORDER BY a.title, r.product_name, r.id",
        (act_id,),
    )
    return [return_view(row) for row in rows]


def quantity(row: dict) -> int:
    return int(row.get("quantity") or 0)


def sync(account: dict, full: bool = False) -> dict:
    """Обновить возвраты кабинета и сказать словами, что изменилось."""
    result = sync_returns(account, full=full)
    parts = [f"Обновлено возвратов: {result.get('returns', 0)}"]
    if result.get("returns_gone"):
        parts.append(f"ушло из выдачи: {result['returns_gone']}")
    result["message"] = ". ".join(parts)
    return result


def giveout(account: dict) -> bytes:
    """Штрихкод Ozon на выдачу возвратов (FBS) — документ площадки."""
    return ozon.get_client(account).giveout_pdf()


def pdf_table(pdf, items: list[dict], everywhere: bool) -> None:
    """Таблица возвратов Ozon на листе PDF — те же колонки, что в HTML-листе."""
    import io

    headings = ["№"]
    widths = [8.0]
    if everywhere:
        headings.append("Кабинет")
        widths.append(22.0)
    headings += ["Возврат", "Схема", "Штрихкод", "Товар", "Артикул / SKU", "Кол-во",
                 "Пункт выдачи", "Отметка", "Комментарий"]
    widths += [30.0, 12.0, 32.0, 48.0, 24.0, 14.0, 36.0, 20.0, 33.0]
    # Раскладываем по ширине листа: иначе на A4 таблица уезжает за поля.
    free = pdf.w - pdf.l_margin - pdf.r_margin
    scale = free / sum(widths)
    widths = [width * scale for width in widths]

    pdf.set_font("sheet", "", 7)
    with pdf.table(col_widths=tuple(widths), first_row_as_headings=True,
                   line_height=4, padding=1.2, repeat_headings=1) as table:
        head = table.row()
        for name in headings:
            head.cell(name)
        for index, item in enumerate(items, start=1):
            row = table.row()
            row.cell(str(index))
            if everywhere:
                row.cell(cut(item.get("account_title") or "—", 22))
            barcode = str(item.get("barcode") or "").strip()
            # Номер штрихкода печатаем рядом с самим кодом: не считался сканером —
            # в пункте выдачи набьют руками, а не поедут за листом заново.
            row.cell("\n".join(x for x in (str(item.get("id")), item.get("order_number"), barcode) if x))
            row.cell(item.get("type") or item.get("scheme") or "—")
            if barcode:
                row.cell(img=io.BytesIO(barcode_svg(barcode)), img_fill_width=True)
            else:
                row.cell("—")
            row.cell(cut(item.get("product_name") or "Без названия", 70))
            row.cell(f"{item.get('offer_id') or '—'}\n{item.get('sku') or ''}".strip())
            row.cell(str(item.get("quantity") or ""))
            row.cell(cut(
                " · ".join(x for x in (item.get("place_name"), item.get("place_address")) if x) or "—", 60
            ))
            row.cell(mark_cell(item))
            row.cell(cut(item.get("note"), 60))


SOURCE = ReturnsSource(
    table="returns",
    label="Ozon",
    unit="поз.",
    ready=ready,
    count_ready=count_ready,
    page=page,
    act_rows=act_rows,
    quantity=quantity,
    list_template="ozon/returns_list.html",
    sheet_template="ozon/returns_sheet.html",
    act_template="ozon/returns_act_rows.html",
    pdf_table=pdf_table,
    sync=sync,
    received=received_returns,
    claim=claim,
    giveout=giveout,
)
