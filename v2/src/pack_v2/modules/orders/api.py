from dataclasses import asdict
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from pack_v2.web.deps import current, manager
from pack_v2.web.query import SYNC_STATUSES, AccountFilter, SyncRequest, money, msk
from pack_v2.web.views import render

STATES = {
    "all": ("Все отправления", ""),
    "awaiting_assembly": ("Ожидает сборки", "warn"),
    "awaiting_dispatch": ("Ожидает отгрузки", "accent"),
    "in_delivery": ("В доставке", ""),
    "delivered": ("Доставлен", "ok"),
    "cancelled": ("Отменён", ""),
    "split": ("Разделён", ""),
    "unknown": ("Уточняется", "warn"),
    "review": ("Нужна проверка", "warn"),
}
FIELDS = {
    "cis": "Честный знак",
    "gtd": "ГТД",
    "rnpt": "РНПТ",
    "weight": "Вес",
    "imei": "IMEI",
    "uin": "УИН",
    "country": "Страна",
}


def build_router():
    router = APIRouter()

    @router.get("/orders")
    def orders(
        request: Request,
        account_id: AccountFilter = None,
        q: Annotated[str, Query(max_length=512)] = "",
        state: str = "all",
        page: Annotated[int, Query(ge=1, le=10000)] = 1,
    ):
        saved = current(request)
        saved.user.require("orders")
        service = request.app.state.orders
        result = service.list_shipments(saved.user, account_id=account_id, query=q, state=state, page=page)
        accounts = request.app.state.connections.account_labels(saved.user, section="orders")

        def page_url(number):
            params = {"q": q, "state": state, "page": number}
            if account_id:
                params["account_id"] = str(account_id)
            return "/orders?" + urlencode(params)

        return render(
            request,
            "orders.html",
            saved=saved,
            title="Заказы",
            active_tab="orders",
            result=result,
            accounts=accounts,
            account_names={a.id: a.title for a in accounts},
            account_markets={
                a.id: "Яндекс Маркет" if a.marketplace == "yandex" else "Ozon" for a in accounts
            },
            selected_account=account_id,
            q=q,
            state=state,
            states=STATES,
            page_url=page_url,
            msk=msk,
            queued=request.query_params.get("queued"),
            syncs=service.sync_status(saved.user),
            sync_statuses=SYNC_STATUSES,
            sync_endpoint="/api/v2/orders/sync",
            sync_refresh="orders-refresh",
            sync_unit="Отправлений",
            sync_caption="Последняя успешная загрузка",
        )

    @router.get("/orders/{shipment_id}")
    def detail(request: Request, shipment_id: UUID):
        saved = current(request)
        result = request.app.state.orders.shipment(saved.user, shipment_id)
        accounts = request.app.state.connections.account_labels(saved.user, section="orders")
        return render(
            request,
            "order_detail.html",
            saved=saved,
            title=result.shipment.posting_number,
            active_tab="orders",
            detail=result,
            states=STATES,
            fields=FIELDS,
            money=money,
            msk=msk,
            account_names={a.id: a.title for a in accounts},
            account_markets={
                a.id: "Яндекс Маркет" if a.marketplace == "yandex" else "Ozon" for a in accounts
            },
        )

    @router.get("/api/v2/orders")
    def data(
        request: Request,
        account_id: AccountFilter = None,
        q: Annotated[str, Query(max_length=512)] = "",
        state: str = "all",
        page: Annotated[int, Query(ge=1, le=10000)] = 1,
    ):
        return asdict(
            request.app.state.orders.list_shipments(
                current(request).user, account_id=account_id, query=q, state=state, page=page
            )
        )

    @router.get("/api/v2/orders/sync")
    def status(request: Request):
        return [asdict(r) for r in request.app.state.orders.sync_status(current(request).user)]

    @router.get("/api/v2/orders/{shipment_id}")
    def data_detail(request: Request, shipment_id: UUID):
        return asdict(request.app.state.orders.shipment(current(request).user, shipment_id))

    @router.post("/api/v2/orders/sync", status_code=202)
    def sync(request: Request, payload: SyncRequest):
        saved = manager(request, request.headers.get("x-csrf-token", ""))
        return {"run_ids": request.app.state.orders.request_sync(saved.user, payload.account_id)}

    @router.post("/orders/sync")
    def sync_form(
        request: Request, csrf: Annotated[str, Form()], account_id: Annotated[AccountFilter, Form()] = None
    ):
        saved = manager(request, csrf)
        ids = request.app.state.orders.request_sync(saved.user, account_id)
        params = {"queued": str(len(ids))}
        if account_id:
            params["account_id"] = str(account_id)
        return RedirectResponse("/orders?" + urlencode(params), 303)

    return router
