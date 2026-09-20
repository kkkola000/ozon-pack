"""Фоновая синхронизация: отправления, товары и возвраты Ozon, заказы Avito.

Синхронизация идёт по всем включённым кабинетам: у каждого свои ключи, свой
клиент API и своя часть данных в общих таблицах (account_id).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import accounts, avito, db, ozon, return_acts, store
from .avito import AvitoError
from .config import settings
from .ozon import OzonError, iso_moment

log = logging.getLogger("sync")

PAGE_LIMIT = 500
RETURNS_PAGE_LIMIT = 500
RETURNS_MAX_PAGES = 40


def _iso_window(days_back: int, days_forward: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days_back), now + timedelta(days=days_forward)


def _account(account: dict | None) -> dict | None:
    return account if account is not None else accounts.default_account()


def sync_postings(account: dict | None = None) -> dict:
    """Забрать отправления в рабочих статусах и освежить те, что из них ушли."""
    account = _account(account)
    if account is None:
        return {"saved": 0, "refreshed": 0}
    account_id = account["id"]
    client = ozon.get_client(account)
    since, to = _iso_window(settings.sync_days_back, settings.sync_days_forward)
    seen: set[str] = set()
    saved = 0

    for status in store.WORK_STATUSES:
        offset = 0
        while True:
            postings, has_next = client.posting_list(status, since, to, limit=PAGE_LIMIT, offset=offset)
            if postings:
                with db.write() as conn:
                    for raw in postings:
                        store.upsert_posting(conn, account_id, raw)
                        seen.add(raw["posting_number"])
                        saved += 1
            if not has_next or not postings:
                break
            offset += PAGE_LIMIT

    # Отправление могло уехать в «Доставляется» или отмениться — узнаём точный статус.
    stale = db.query(
        "SELECT posting_number FROM postings WHERE account_id = ? AND status IN (?, ?)",
        (account_id, *store.WORK_STATUSES),
    )
    refreshed = 0
    for row in stale:
        number = row["posting_number"]
        if number in seen:
            continue
        try:
            raw = client.posting_get(number)
        except OzonError as exc:
            log.warning("Не удалось обновить %s: %s", number, exc)
            continue
        if raw:
            with db.write() as conn:
                store.upsert_posting(conn, account_id, raw)
            refreshed += 1
        else:
            db.execute(
                "UPDATE postings SET status = 'unknown', updated_at = ? WHERE account_id = ? AND posting_number = ?",
                (db.now_iso(), account_id, number),
            )
    return {"saved": saved, "refreshed": refreshed}


def sync_products(account: dict | None = None, limit: int = 500) -> dict:
    """Подтянуть карточки товаров (штрихкоды и фото) для новых SKU."""
    account = _account(account)
    if account is None:
        return {"products": 0}
    account_id = account["id"]
    rows = db.query(
        """
        SELECT DISTINCT i.sku FROM posting_items i
        LEFT JOIN products p ON p.sku = i.sku AND p.account_id = i.account_id
        WHERE i.account_id = ? AND p.sku IS NULL
        UNION
        SELECT DISTINCT r.sku FROM returns r
        LEFT JOIN products p2 ON p2.sku = r.sku AND p2.account_id = r.account_id
        WHERE r.account_id = ? AND p2.sku IS NULL AND r.sku IS NOT NULL
        LIMIT ?
        """,
        (account_id, account_id, limit),
    )
    skus = [row["sku"] for row in rows if row["sku"]]
    if not skus:
        return {"products": 0}

    client = ozon.get_client(account)
    total = 0
    for start in range(0, len(skus), 100):
        chunk = skus[start : start + 100]
        try:
            items = client.product_info(skus=chunk)
        except OzonError as exc:
            log.warning("Карточки товаров недоступны: %s", exc)
            break
        if items:
            with db.write() as conn:
                total += store.upsert_products(conn, account_id, items)
        # SKU без карточки (например, товар архивирован) — чтобы не спрашивать бесконечно.
        found = {str(item.get("sku") or item.get("id")) for item in items}
        missing = [sku for sku in chunk if sku not in found]
        if missing:
            with db.write() as conn:
                for sku in missing:
                    name = db.query_one(
                        "SELECT name, offer_id FROM posting_items WHERE account_id = ? AND sku = ? LIMIT 1",
                        (account_id, sku),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO products(account_id, sku, offer_id, name, barcodes, updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (account_id, sku, (name["offer_id"] if name else None),
                         (name["name"] if name else None), "[]", db.now_iso()),
                    )
    return {"products": total}


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
    from .options import get_received_days, get_returns_statuses, wanted_statuses

    account = _account(account)
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
    histogram: dict[str, int] = {}
    complete = True
    # Полнота обхода «к выдаче» считается отдельно: по ней список выдачи
    # пересобирается заново, и сбой на выборке полученных не должен этому
    # мешать — это разные запросы к разным статусам.
    pickup_complete = True

    def remember(raw: dict) -> None:
        """Учитываем, что именно вернул Ozon, — histogram виден в интерфейсе."""
        nonlocal skipped
        sys_name = (((raw.get("visual") or {}).get("status") or {}).get("sys_name")) or "—"
        histogram[sys_name] = histogram.get(sys_name, 0) + 1
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
                    seen.add(store.upsert_return(conn, account_id, raw))
                    saved += 1
        return len(keep)

    if full:
        # Полный обход без фильтра — чтобы увидеть, что вообще есть в Ozon.
        last_id = 0
        for _page in range(RETURNS_MAX_PAGES):
            try:
                returns, has_next = client.returns_list(limit=RETURNS_PAGE_LIMIT, last_id=last_id)
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
            for _page in range(RETURNS_MAX_PAGES):
                try:
                    # В фильтре /v1/returns/list допускается только одно поле,
                    # поэтому и статусы, и окно запрашиваем по очереди.
                    returns, has_next = client.returns_list(
                        limit=RETURNS_PAGE_LIMIT, last_id=last_id, filter_=filter_
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
                what, RETURNS_MAX_PAGES,
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
            since, until = _iso_window(received_days, 1)
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

    db.kv_set("returns_last_statuses", json.dumps(histogram, ensure_ascii=False))
    db.kv_set("returns_last_wanted", ",".join(wanted))
    result = {"returns": saved}
    if skipped:
        result["returns_skipped"] = skipped
    if gone:
        result["returns_gone"] = gone
    if removed:
        result["returns_removed"] = removed
    return result


def sync_avito(account: dict | None = None) -> dict:
    """Заказы Avito, с которыми сборщику надо что-то сделать.

    Это «ожидает подтверждения», «ждёт отправки» и возвраты. Из возвратов
    сохраняются только те, что уже лежат в пункте выдачи (returnStatus =
    ready_to_pickup): пока посылка едет обратно, забирать нечего, и в панели
    ей делать нечего. Остальные статусы заказа не запрашиваются вовсе —
    уехавший в «в пути» или «доставлен» заказ из панели просто исчезает.
    """
    account = _account(account)
    if account is None:
        return {"avito": 0}
    account_id = account["id"]
    client = avito.get_client(account)
    date_from = datetime.now(timezone.utc) - timedelta(days=settings.avito_days_back)

    seen: set[str] = set()
    saved = 0
    # Что именно вернул Avito по возвратам — видно на странице возвратов.
    returns_seen: dict[str, int] = {}
    try:
        # Два запроса, а не один. dateFrom у Avito отсекает по дате СОЗДАНИЯ
        # заказа: покупку сделали два месяца назад, вернули сегодня — с окном
        # в 30 дней такой возврат в панель бы не попал. Плюс возвраты не
        # должны конкурировать с текущими заказами за страницы выдачи.
        orders = client.orders_all(statuses=list(avito.WORK_STATUSES), date_from=date_from)
        orders += client.orders_all(statuses=list(avito.RETURN_STATUSES))
    except AvitoError as exc:
        log.warning("Заказы Avito недоступны: %s", exc)
        raise

    keep = []
    for raw in orders:
        if raw.get("status") != avito.STATUS_ON_RETURN:
            keep.append(raw)
            continue
        return_status = ((raw.get("returnPolicy") or {}).get("returnStatus")) or "—"
        returns_seen[return_status] = returns_seen.get(return_status, 0) + 1
        # Забрать можно только то, что доехало до пункта выдачи.
        if avito.is_ready_for_pickup(return_status):
            keep.append(raw)
            if not avito.pickup_address(raw):
                # Адрес ПВЗ Avito отдаёт не всегда — видно, где его искать дальше.
                log.info(
                    "Возврат %s без адреса ПВЗ; delivery: %s, returnPolicy: %s",
                    raw.get("marketplaceId") or raw.get("id"),
                    sorted((raw.get("delivery") or {}).keys()),
                    sorted((raw.get("returnPolicy") or {}).keys()),
                )

    if keep:
        with db.write() as conn:
            for raw in keep:
                seen.add(store.upsert_avito_order(conn, account_id, raw))
                saved += 1
    db.kv_set(f"avito_returns_statuses:{account_id}", json.dumps(returns_seen, ensure_ascii=False))

    # Заказ ушёл из рабочих статусов — Avito его больше не отдаёт, убираем и мы.
    stale = [
        row["id"]
        for row in db.query("SELECT id FROM avito_orders WHERE account_id = ?", (account_id,))
        if row["id"] not in seen
    ]
    if stale:
        placeholders = ",".join("?" for _ in stale)
        with db.write() as conn:
            conn.execute(
                f"DELETE FROM avito_order_items WHERE account_id = ? AND order_id IN ({placeholders})",
                [account_id] + stale,
            )
            conn.execute(
                f"DELETE FROM avito_orders WHERE account_id = ? AND id IN ({placeholders})",
                [account_id] + stale,
            )
    result = {"avito": saved}
    ready = db.query_one(
        "SELECT COUNT(*) AS c FROM avito_orders WHERE account_id = ? AND status = ?",
        (account_id, avito.STATUS_ON_RETURN),
    )["c"]
    if ready:
        result["avito_returns"] = ready
    skipped = sum(count for code, count in returns_seen.items() if not avito.is_ready_for_pickup(code))
    if skipped:
        result["avito_returns_skipped"] = skipped
    if stale:
        result["avito_gone"] = len(stale)
    return result


def sync_account(account: dict, *, returns: bool = True) -> dict:
    """Один кабинет: набор методов зависит от площадки."""
    if account["marketplace"] == "avito":
        return sync_avito(account)
    result: dict = {}
    result.update(sync_postings(account))
    result.update(sync_products(account))
    if returns:
        result.update(sync_returns(account))
    return result


def sync_all(*, returns: bool = True) -> dict:
    """Все включённые кабинеты. Ошибка одного не останавливает остальные."""
    result: dict = {}
    errors: list[str] = []
    # Без ключей запрашивать нечего — и придумывать данные панель не станет.
    active = [a for a in accounts.all_accounts(active_only=True) if accounts.is_configured(a)]
    for account in active:
        try:
            part = sync_account(account, returns=returns)
        except Exception as exc:  # noqa: BLE001 - кабинет мог остаться без ключей
            log.warning("Кабинет «%s» не синхронизирован: %s", account["title"], exc)
            errors.append(f"{account['title']}: {exc}")
            continue
        for key, value in part.items():
            if isinstance(value, int) and isinstance(result.get(key), int):
                result[key] += value
            else:
                result[key] = value
    if len(active) > 1:
        result["accounts"] = len(active)
    if errors:
        result["errors"] = errors
        # Все кабинеты упали — это уже отказ синхронизации, а не частный сбой.
        if len(errors) == len(active):
            raise RuntimeError("; ".join(errors))
    return result


def run_once(*, returns: bool = True, account: dict | None = None) -> dict:
    """Один проход синхронизации с записью статуса в kv.

    Без account обходит все включённые кабинеты (так работает фоновый поток);
    с account обновляет только его — это кнопка «Обновить» в интерфейсе.
    """
    started = time.time()
    try:
        result = sync_account(account, returns=returns) if account else sync_all(returns=returns)
    except Exception as exc:  # noqa: BLE001 - статус нужен в UI целиком
        log.exception("Синхронизация упала")
        db.kv_set("sync_last_error", f"{db.now_iso()}: {exc}")
        db.kv_set("sync_last_status", "error")
        raise
    db.kv_set("sync_last_ok", db.now_iso())
    db.kv_set("sync_last_status", "ok")
    db.kv_set("sync_last_error", "")
    db.kv_set("sync_last_result", str(result))
    db.kv_set("sync_last_duration", f"{time.time() - started:.1f}")
    return result


class SyncWorker(threading.Thread):
    """Отдельный поток: отправления часто, возвраты реже."""

    daemon = True

    def __init__(self) -> None:
        super().__init__(name="ozon-sync")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_returns = 0.0

    def request_sync(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:  # pragma: no cover - фоновой поток
        while not self._stop.is_set():
            with_returns = time.time() - self._last_returns > settings.sync_returns_interval
            try:
                run_once(returns=with_returns)
                if with_returns:
                    self._last_returns = time.time()
            except Exception:  # noqa: BLE001 - поток не должен умирать
                # Поток переживает любую ошибку, но молчать о ней нельзя: сбой
                # в самой записи статуса иначе исчезает бесследно.
                log.exception("Проход синхронизации сорвался")
            self._wake.wait(timeout=settings.sync_interval)
            self._wake.clear()


_worker: SyncWorker | None = None


def start_worker() -> SyncWorker | None:
    global _worker
    if not settings.sync_enabled:
        log.info("Фоновая синхронизация отключена (SYNC_ENABLED=0)")
        return None
    if _worker is None:
        _worker = SyncWorker()
        _worker.start()
    return _worker


def get_worker() -> SyncWorker | None:
    return _worker


def status() -> dict:
    active = accounts.all_accounts(active_only=True)
    return {
        "last_ok": db.kv_get("sync_last_ok"),
        "last_status": db.kv_get("sync_last_status", "never"),
        "last_error": db.kv_get("sync_last_error"),
        "last_result": db.kv_get("sync_last_result"),
        "duration": db.kv_get("sync_last_duration"),
        "interval": settings.sync_interval,
        "enabled": settings.sync_enabled,
        # Кабинеты без ключей синхронизировать нечем — о них говорим отдельно.
        "unconfigured": [a["title"] for a in active if not accounts.is_configured(a)],
    }
