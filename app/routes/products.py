"""Раздел «Товары»: каталог кабинета и наборы.

Каталог панель тянет у площадки сама (sync_products), править его здесь нечего.
Смотреть — нужно: по штрихкоду с полки видно, что это за товар и в какие
наборы он входит.

Наборы — то, чего у площадки нет. На Ozon набор обычный товар с одним SKU, а на
складе его собирают из нескольких вещей со своими штрихкодами. Состав задаётся
здесь и живёт только в панели.

Смотреть раздел может тот, кому он выдан галочкой. Менять наборы и перечитывать
каталог — владелец и администратор: состав набора меняет то, как панель
засчитывает сборку, и ошибка здесь тихо испортит проверку на складе.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core import catalog, db, product_sets
from ..core.deps import check_csrf, require_manager, require_section, require_ozon_account, templates

router = APIRouter()

PAGE_LIMIT = 200


def _view(row: dict) -> dict:
    """Строка каталога для экрана: штрихкоды разворачиваем из JSON."""
    item = dict(row)
    try:
        item["barcodes"] = json.loads(item.get("barcodes") or "[]")
    except ValueError:
        item["barcodes"] = []
    return item


def search(account_id: int, q: str = "", limit: int = PAGE_LIMIT) -> list[dict]:
    """Живые товары кабинета: по названию, артикулу или штрихкоду.

    Архив не показываем: такой товар не продаётся, и в выборе для набора он
    только мешает — их бывает больше, чем живых. Строка при этом остаётся в
    базе, чтобы штрихкод архивного товара продолжал сканироваться, если тот
    застрял в несобранном заказе.

    Штрихкод ищется через справочник, а не по колонке barcodes: там JSON, и
    LIKE по нему находил бы куски чужих кодов.
    """
    conditions = ["p.account_id = ?", "p.archived = 0"]
    params: list = [account_id]
    if q.strip():
        like = f"%{q.strip()}%"
        conditions.append(
            "(p.name LIKE ? OR p.offer_id LIKE ? OR p.sku LIKE ? OR p.sku IN "
            "(SELECT sku FROM product_barcodes WHERE account_id = ? AND barcode LIKE ?))"
        )
        params += [like, like, like, account_id, like]
    rows = db.query(
        f"SELECT p.* FROM products p WHERE {' AND '.join(conditions)} "
        f"ORDER BY (p.name IS NULL), p.name LIMIT ?",
        params + [limit],
    )
    return [_view(row) for row in rows]


@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, q: str = "", tab: str = "catalog",
                  user: dict = Depends(require_section("products")),
                  account: dict = Depends(require_ozon_account)):
    """Каталог кабинета и наборы."""
    aid = account["id"]
    items = search(aid, q)
    sets = product_sets.all_sets(aid)
    # Товар уже набор — во второй раз его предлагать не надо.
    taken = {item["sku"] for item in sets}
    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "items": items,
            "sets": sets,
            "taken": taken,
            "q": q,
            "tab": "sets" if tab == "sets" else "catalog",
            "total": db.query_one(
                "SELECT COUNT(*) AS c FROM products WHERE account_id = ? AND archived = 0", (aid,)
            )["c"],
            "archived": db.query_one(
                "SELECT COUNT(*) AS c FROM products WHERE account_id = ? AND archived = 1", (aid,)
            )["c"],
            "job": catalog.job_status(aid),
            "truncated": len(items) >= PAGE_LIMIT,
            "csrf": request.state.session.get("csrf"),
            "active_tab": "products",
        },
    )


@router.post("/api/products/catalog/refresh")
def api_refresh_catalog(request: Request, admin: dict = Depends(require_manager),
                        account: dict = Depends(require_ozon_account)):
    """Перечитать каталог кабинета у Ozon целиком.

    Обычная синхронизация тянет только товары из заказов и возвратов — для
    набора этого мало. Обход идёт в фоне: тысячи карточек за один запрос
    браузера не успеть.
    """
    check_csrf(request)
    return catalog.start(account, admin)


@router.get("/api/products/catalog/status")
def api_catalog_status(admin: dict = Depends(require_section("products")),
                       account: dict = Depends(require_ozon_account)):
    """Как идёт обход — кнопка спрашивает, пока он не закончится."""
    return catalog.job_status(account["id"])


@router.get("/api/products/search")
def api_search(q: str = "", limit: int = 20, admin: dict = Depends(require_section("products")),
               account: dict = Depends(require_ozon_account)):
    """Подсказка при выборе товара: и для набора, и для его частей."""
    found = search(account["id"], q, limit=max(1, min(limit, 50)))
    return {
        "items": [
            {"sku": item["sku"], "offer_id": item["offer_id"], "name": item["name"],
             "image": item["image"], "barcodes": item["barcodes"]}
            for item in found
        ]
    }


@router.get("/api/products/{sku}")
def api_product(sku: str, admin: dict = Depends(require_section("products")),
                account: dict = Depends(require_ozon_account)):
    """Карточка товара вместе с тем, в какие наборы он входит."""
    row = db.query_one(
        "SELECT * FROM products WHERE account_id = ? AND sku = ?", (account["id"], sku)
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Товара {sku} нет в каталоге кабинета")
    return {
        "product": _view(row),
        "set": product_sets.get(account["id"], sku),
        "part_of": product_sets.parents_of(account["id"], sku=sku),
    }


@router.post("/api/products/sets")
def api_save_set(request: Request, payload: dict = Body(...),
                 admin: dict = Depends(require_manager),
                 account: dict = Depends(require_ozon_account)):
    """Создать набор или переписать его состав."""
    check_csrf(request)
    try:
        saved = product_sets.save(
            account["id"],
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


@router.delete("/api/products/sets/{sku}")
def api_delete_set(sku: str, request: Request, admin: dict = Depends(require_manager),
                   account: dict = Depends(require_ozon_account)):
    """Убрать набор. Товар остаётся — просто собирается по своему штрихкоду."""
    check_csrf(request)
    if not product_sets.delete(account["id"], sku, user=admin):
        raise HTTPException(status_code=404, detail="Набор не найден")
    return {"status": "ok", "message": "Набор удалён: товар снова обычный"}
