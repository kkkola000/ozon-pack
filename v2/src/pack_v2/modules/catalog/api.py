from dataclasses import asdict
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict

from pack_v2.web.deps import current, manager
from pack_v2.web.views import render

AccountFilter = Annotated[UUID | None, BeforeValidator(lambda value: None if value == "" else value)]

STATES = {
    "all": "Все товары",
    "active": "Активные",
    "archived": "В архиве",
    "missing": "Нет в последней загрузке",
}
SYNC_STATUSES = {
    "queued": ("В очереди", ""),
    "running": ("Загружается", "accent"),
    "retrying": ("Ожидает повтора", "warn"),
    "succeeded": ("Загружено", "ok"),
    "failed": ("Не завершено", "err"),
    "stale": ("Требует обновления", "warn"),
}


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: AccountFilter = None


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
            syncs=catalog.sync_status(saved.user),
            sync_statuses=SYNC_STATUSES,
            states=STATES,
            selected_account=account_id,
            q=q,
            state=state,
            page_url=page_url,
            queued=request.query_params.get("queued"),
            msk=lambda value: (
                value.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M") if value else "—"
            ),
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
