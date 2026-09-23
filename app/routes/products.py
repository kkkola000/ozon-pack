"""Раздел «Товары»: каталог всех кабинетов, наборы и сопоставление.

Раздел один на всю панель, а не на кабинет. Склад общий: коробка на полке одна,
а продаётся она на трёх площадках, и сборщик сканирует её штрихкод независимо
от того, чей заказ перед ним. Поэтому и каталог показываем целиком, с фильтром
по кабинетам.

Три вкладки:
  каталог        — что вообще есть на складе: фото, артикул, название, штрихкод;
  наборы         — из каких частей собирается товар площадки;
  сопоставление  — какие карточки разных кабинетов на самом деле один товар.

Каталог панель тянет у площадки сама, править его здесь нечего. Смотреть —
нужно: по штрихкоду с полки видно, что это за товар и в какие наборы он входит.

Смотреть раздел может тот, кому он выдан галочкой. Менять наборы, сопоставлять
и перечитывать каталог — владелец и администратор: и состав набора, и связь
карточек меняют то, как панель засчитывает сборку, а ошибка здесь тихо испортит
проверку на складе.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core import accounts, catalog, db, product_links, product_sets
from ..core.deps import (check_csrf, current_account, require_account, require_manager,
                         require_section, templates)

router = APIRouter()

PAGE_LIMIT = 200
TABS = ("catalog", "sets", "match")
# Вкладка сопоставления: что разбираем — новые совпадения, уже сведённое или
# отвергнутое. Три списка на одной странице читались бы как свалка.
VIEWS = ("new", "linked", "skipped")


def require_catalog(request: Request) -> dict:
    """Кабинет площадки, которая умеет наполнять каталог, — для обхода каталога."""
    from ..markets import registry

    account = require_account(request)
    market = registry.get(account["marketplace"])
    if market is None or market.catalog is None:
        title = market.title if market else account["marketplace"]
        raise HTTPException(status_code=409, detail=f"Площадка «{title}» каталог товаров не отдаёт")
    return account


def catalog_accounts() -> list[dict]:
    """Кабинеты, у которых каталог вообще есть, — им можно нажать «Обновить»."""
    from ..markets import registry

    out = []
    for account in accounts.all_accounts():
        market = registry.get(account["marketplace"])
        if market is not None and market.catalog is not None:
            out.append(account)
    return out


def _picked(cab: str) -> list[int]:
    """Выбранные кабинеты из адреса: «cab=1,3». Пусто — значит все."""
    picked = []
    for part in str(cab or "").split(","):
        part = part.strip()
        if part.isdigit():
            picked.append(int(part))
    known = {account["id"] for account in accounts.all_accounts()}
    return [account_id for account_id in picked if account_id in known]


def _view(row: dict) -> dict:
    """Строка каталога для экрана: штрихкоды разворачиваем из JSON."""
    item = dict(row)
    item["barcodes"] = db.json_list(item.get("barcodes"))
    return item


def search(account_ids: list[int] | None = None, q: str = "", limit: int = PAGE_LIMIT) -> list[dict]:
    """Живые товары кабинетов: по названию, артикулу или штрихкоду.

    Архив не показываем: такой товар не продаётся, и в выборе для набора он
    только мешает — их бывает больше, чем живых. Строка при этом остаётся в
    базе, чтобы штрихкод архивного товара продолжал сканироваться, если тот
    застрял в несобранном заказе.

    Штрихкод ищется через справочник, а не по колонке barcodes: там JSON, и
    LIKE по нему находил бы куски чужих кодов.
    """
    conditions = ["p.archived = 0"]
    params: list = []
    if account_ids:
        marks = ",".join("?" for _ in account_ids)
        conditions.append(f"p.account_id IN ({marks})")
        params += list(account_ids)
    if q.strip():
        # Регистр приводим и у колонки, и у запроса: LIKE в SQLite не различает
        # регистр только у латиницы, и «кофе» не нашло бы «Кофе».
        like = f"%{q.strip().lower()}%"
        conditions.append(
            "(lower_ru(p.name) LIKE ? OR lower_ru(p.offer_id) LIKE ? OR lower_ru(p.sku) LIKE ? "
            "OR p.sku IN (SELECT sku FROM product_barcodes "
            "WHERE account_id = p.account_id AND barcode LIKE ?))"
        )
        params += [like, like, like, like]
    rows = db.query(
        f"""
        SELECT p.*, a.title AS shop, a.marketplace AS market, l.group_id, l.is_main
          FROM products p
          JOIN accounts a ON a.id = p.account_id
          LEFT JOIN product_links l ON l.account_id = p.account_id AND l.sku = p.sku
         WHERE {' AND '.join(conditions)}
         ORDER BY (p.name IS NULL), p.name, a.sort, a.id LIMIT ?
        """,
        params + [limit],
    )
    return [_view(row) for row in rows]


def catalog_rows(account_ids: list[int] | None = None, q: str = "",
                 limit: int = PAGE_LIMIT) -> list[dict]:
    """Каталог для экрана: сопоставленный товар занимает одну строку.

    Смысл сопоставления как раз в этом: на складе одна коробка — в списке одна
    строка, а карточки площадок под ней, если их захотят посмотреть.
    """
    found = search(account_ids, q, limit)
    # Совпадения по артикулу считаем один раз: по ним в каталоге видно, где ещё
    # предстоит работа, без похода на вкладку сопоставления.
    matches = {item["article"] for item in product_links.suggestions()}
    rows: list[dict] = []
    seen_groups: set[str] = set()
    for item in found:
        group_id = item.get("group_id")
        if group_id:
            if group_id in seen_groups:
                continue
            seen_groups.add(group_id)
            cards = product_links.cards_of(group_id)
            main = next((card for card in cards if card["is_main"]), cards[0] if cards else item)
            rows.append({**main, "group_id": group_id, "cards": cards, "linked": len(cards)})
            continue
        rows.append({
            **item, "cards": [], "linked": 0,
            "has_match": product_links.article_of(item.get("offer_id")) in matches,
        })
    return rows


@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, q: str = "", tab: str = "catalog", cab: str = "",
                  view: str = "new", user: dict = Depends(require_section("products"))):
    """Каталог всех кабинетов, наборы и сопоставление."""
    tab = tab if tab in TABS else "catalog"
    view = view if view in VIEWS else "new"
    picked = _picked(cab)
    shops = accounts.all_accounts()
    items = catalog_rows(picked, q) if tab == "catalog" else []
    sets = product_sets.all_sets_everywhere(picked) if tab in ("sets", "catalog") else []
    taken = {(item["account_id"], item["sku"]) for item in sets}
    counts = {
        account["id"]: db.query_one(
            "SELECT COUNT(*) AS c FROM products WHERE account_id = ? AND archived = 0",
            (account["id"],),
        )["c"]
        for account in shops
    }
    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "request": request,
            "user": user,
            # Раздел общий, но шапка страницы показывает текущий кабинет.
            "account": current_account(request),
            "items": items,
            "sets": sets,
            "taken": taken,
            "q": q,
            "tab": tab,
            "view": view,
            "shops": shops,
            "picked": picked,
            "counts": counts,
            "total": sum(counts.values()),
            "catalog_shops": catalog_accounts(),
            "links": product_links.stats(picked) if tab != "sets" else product_links.stats(),
            "suggestions": product_links.suggestions(picked) if tab == "match" and view == "new" else [],
            "groups": product_links.groups(picked, q) if tab == "match" and view == "linked" else [],
            "skipped": product_links.skipped() if tab == "match" and view == "skipped" else [],
            "jobs": {account["id"]: catalog.job_status(account["id"]) for account in catalog_accounts()},
            "truncated": len(items) >= PAGE_LIMIT,
            "csrf": request.state.session.get("csrf"),
            "active_tab": "products",
        },
    )


# ------------------------------------------------------------------ каталог
@router.post("/api/products/catalog/refresh")
def api_refresh_catalog(request: Request, payload: dict = Body(default=None),
                        admin: dict = Depends(require_manager)):
    """Перечитать каталог: одного кабинета или сразу всех, у кого он есть.

    Обычная синхронизация тянет только товары из заказов и возвратов — для
    набора этого мало. Обход идёт в фоне: тысячи карточек за один запрос
    браузера не успеть.
    """
    check_csrf(request)
    wanted = _picked(str((payload or {}).get("cab") or "")) or None
    shops = [account for account in catalog_accounts()
             if wanted is None or account["id"] in wanted]
    if not shops:
        raise HTTPException(status_code=409, detail="Ни один кабинет каталог товаров не отдаёт")
    started = [catalog.start(account, admin) for account in shops]
    running = sum(1 for result in started if result["status"] == "started")
    return {
        "status": "started" if running else "running",
        "shops": [account["id"] for account in shops],
        "message": (f"Обновляем каталог: кабинетов {running}" if running
                    else "Каталог уже обновляется"),
    }


@router.get("/api/products/catalog/status")
def api_catalog_status(user: dict = Depends(require_section("products"))):
    """Как идёт обход — кнопка спрашивает, пока он не закончится."""
    jobs = {account["id"]: catalog.job_status(account["id"]) for account in catalog_accounts()}
    return {
        "jobs": jobs,
        "running": any(job.get("running") for job in jobs.values()),
        "errors": [job.get("error") for job in jobs.values() if job.get("status") == "error"],
    }


@router.get("/api/products/search")
def api_search(q: str = "", limit: int = 20, cab: str = "",
               user: dict = Depends(require_section("products"))):
    """Подсказка при выборе товара: и для набора, и для его частей."""
    found = search(_picked(cab), q, limit=max(1, min(limit, 50)))
    return {
        "items": [
            {"account_id": item["account_id"], "shop": item["shop"], "sku": item["sku"],
             "offer_id": item["offer_id"], "name": item["name"], "image": item["image"],
             "barcodes": item["barcodes"], "group_id": item.get("group_id")}
            for item in found
        ]
    }


@router.get("/api/products/{account_id}/{sku}")
def api_product(account_id: int, sku: str, user: dict = Depends(require_section("products"))):
    """Карточка товара: её наборы и с чем она сопоставлена."""
    card = product_links.card_at(account_id, sku)
    if not card:
        raise HTTPException(status_code=404, detail=f"Товара {sku} нет в каталоге кабинета")
    group_id = card.get("group_id")
    return {
        "product": card,
        "set": product_sets.get(account_id, sku),
        "part_of": product_sets.parents_of(account_id, sku=sku),
        "linked": product_links.cards_of(group_id) if group_id else [],
    }


# ------------------------------------------------------------------ наборы
@router.post("/api/products/sets")
def api_save_set(request: Request, payload: dict = Body(...),
                 admin: dict = Depends(require_manager)):
    """Создать набор или переписать его состав."""
    check_csrf(request)
    account_id = int(payload.get("account_id") or 0) or require_account(request)["id"]
    try:
        saved = product_sets.save(
            account_id,
            str(payload.get("sku") or ""),
            list(payload.get("parts") or []),
            title=str(payload.get("title") or ""),
            user=admin,
        )
    except product_sets.SetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "set": saved,
        "message": f"Набор сохранён: частей {len(saved['parts'])}",
    }


@router.delete("/api/products/sets/{account_id}/{sku}")
def api_delete_set(account_id: int, sku: str, request: Request,
                   admin: dict = Depends(require_manager)):
    """Убрать набор. Товар остаётся — просто собирается по своему штрихкоду."""
    check_csrf(request)
    if not product_sets.delete(account_id, sku, user=admin):
        raise HTTPException(status_code=404, detail="Набор не найден")
    return {"status": "ok", "message": "Набор удалён: товар снова обычный"}


# ------------------------------------------------------------------ сопоставление
@router.post("/api/products/links/confirm")
def api_confirm_link(request: Request, payload: dict = Body(...),
                     admin: dict = Depends(require_manager)):
    """Подтвердить совпадение по артикулу — или все сразу."""
    check_csrf(request)
    if payload.get("all"):
        done = product_links.confirm_all(_picked(str(payload.get("cab") or "")) or None, user=admin)
        return {"status": "ok", "linked": done,
                "message": f"Сопоставлено товаров: {done}" if done else "Подтверждать нечего"}
    try:
        result = product_links.confirm(str(payload.get("article") or ""), user=admin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "group_id": result["group_id"],
            "message": f"Сопоставлено карточек: {len(result['cards'])}"}


@router.post("/api/products/links")
def api_link(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    """Ручное сопоставление: основной товар и привязанные к нему карточки."""
    check_csrf(request)
    main = payload.get("main") or {}
    others = [
        (int(card.get("account_id") or 0), str(card.get("sku") or ""))
        for card in (payload.get("cards") or [])
    ]
    if not main.get("sku") or not others:
        raise HTTPException(status_code=400, detail="Нужен основной товар и хотя бы одна карточка к нему")
    try:
        result = product_links.link(
            (int(main.get("account_id") or 0), str(main.get("sku"))), others, user=admin
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "group_id": result["group_id"],
            "message": f"Сопоставлено карточек: {len(result['cards'])}"}


@router.delete("/api/products/links/{group_id}")
def api_unlink(group_id: str, request: Request, admin: dict = Depends(require_manager)):
    """Отменить сопоставление: карточки снова становятся отдельными товарами."""
    check_csrf(request)
    removed = product_links.unlink(group_id, user=admin)
    if not removed:
        raise HTTPException(status_code=404, detail="Такого сопоставления нет")
    return {
        "status": "ok",
        "message": f"Сопоставление отменено: карточек {removed}. "
                   "Совпадение по артикулу вернётся в предложения.",
    }


@router.delete("/api/products/links/{account_id}/{sku}")
def api_unlink_card(account_id: int, sku: str, request: Request,
                    admin: dict = Depends(require_manager)):
    """Вынуть одну карточку из сопоставления, не трогая остальные."""
    check_csrf(request)
    if not product_links.unlink_card(account_id, sku, user=admin):
        raise HTTPException(status_code=404, detail="Эта карточка ни с чем не сопоставлена")
    return {"status": "ok", "message": "Карточка больше не сопоставлена"}


@router.post("/api/products/links/main")
def api_set_main(request: Request, payload: dict = Body(...),
                 admin: dict = Depends(require_manager)):
    """Сделать карточку основной: её название и фото видно в каталоге."""
    check_csrf(request)
    account_id = int(payload.get("account_id") or 0)
    sku = str(payload.get("sku") or "")
    if not product_links.set_main(account_id, sku, user=admin):
        raise HTTPException(status_code=404, detail="Эта карточка ни с чем не сопоставлена")
    return {"status": "ok", "message": "Основной товар изменён"}


@router.post("/api/products/links/skip")
def api_skip(request: Request, payload: dict = Body(...), admin: dict = Depends(require_manager)):
    """«Не сопоставлять»: артикул уходит из предложений. И обратно — тоже здесь."""
    check_csrf(request)
    article = str(payload.get("article") or "")
    if payload.get("undo"):
        product_links.unskip(article, user=admin)
        return {"status": "ok", "message": f"Артикул {article} снова в предложениях"}
    product_links.skip(article, user=admin)
    return {"status": "ok", "message": f"Артикул {article} больше не предлагается"}
