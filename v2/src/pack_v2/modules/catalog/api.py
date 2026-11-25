from dataclasses import asdict
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from pack_v2.web.deps import current, manager
from pack_v2.web.query import SYNC_STATUSES, AccountFilter, SyncRequest, msk
from pack_v2.web.views import render

STATES = {
    "all": "Все товары",
    "active": "Активные",
    "archived": "В архиве",
    "missing": "Нет в последней загрузке",
}


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/products")
    def products(
        request: Request,
        account_id: AccountFilter = None,
        q: Annotated[str, Query(max_length=512)] = "",
        state: Literal["all", "active", "archived", "missing"] = "all",
        page: Annotated[int, Query(ge=1, le=10000)] = 1,
    ):
        saved = current(request)
        saved.user.require("products")
        catalog = request.app.state.catalog
        result = catalog.list_offers(saved.user, account_id=account_id, query=q, state=state, page=page)
        accounts = request.app.state.connections.account_labels(saved.user)

        def page_url(number):
            params = {"q": q, "state": state, "page": number}
            if account_id:
                params["account_id"] = str(account_id)
            return "/products?" + urlencode(params)

        return render(
            request,
            "products.html",
            saved=saved,
            title="Товары",
            active_tab="products",
            result=result,
            accounts=accounts,
            account_names={a.id: a.title for a in accounts},
            account_markets={
                a.id: "Яндекс Маркет" if a.marketplace == "yandex" else "Ozon" for a in accounts
            },
            syncs=catalog.sync_status(saved.user),
            sync_statuses=SYNC_STATUSES,
            states=STATES,
            selected_account=account_id,
            q=q,
            state=state,
            page_url=page_url,
            queued=request.query_params.get("queued"),
            msk=msk,
        )

    @router.get("/api/v2/products")
    def product_data(
        request: Request,
        account_id: AccountFilter = None,
        q: Annotated[str, Query(max_length=512)] = "",
        state: Literal["all", "active", "archived", "missing"] = "all",
        page: Annotated[int, Query(ge=1, le=10000)] = 1,
    ):
        saved = current(request)
        return asdict(
            request.app.state.catalog.list_offers(
                saved.user, account_id=account_id, query=q, state=state, page=page
            )
        )

    @router.get("/api/v2/catalog/sync")
    def sync_status(request: Request):
        return [asdict(r) for r in request.app.state.catalog.sync_status(current(request).user)]

    @router.post("/api/v2/catalog/sync", status_code=202)
    def sync(request: Request, payload: SyncRequest):
        saved = manager(request, request.headers.get("x-csrf-token", ""))
        return {"run_ids": request.app.state.catalog.request_sync(saved.user, payload.account_id)}

    @router.post("/products/sync")
    def sync_form(
        request: Request, csrf: Annotated[str, Form()], account_id: Annotated[AccountFilter, Form()] = None
    ):
        saved = manager(request, csrf)
        selected = account_id
        ids = request.app.state.catalog.request_sync(saved.user, selected)
        params = {"queued": str(len(ids))}
        if selected:
            params["account_id"] = str(selected)
        return RedirectResponse("/products?" + urlencode(params), 303)

    return router
