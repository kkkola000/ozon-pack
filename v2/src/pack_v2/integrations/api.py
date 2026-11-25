from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, model_validator

from pack_v2.shared.errors import DomainError
from pack_v2.web.deps import manager
from pack_v2.web.views import settings_page


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=0, strict=True)


class CreateAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    marketplace: Literal["ozon", "yandex"] = "ozon"
    title: str = Field(min_length=1, max_length=200)
    client_id: str | None = Field(default=None, min_length=1, max_length=32)
    business_id: str | None = Field(default=None, min_length=1, max_length=32)
    api_key: SecretStr

    @model_validator(mode="after")
    def identity(self):
        if (self.marketplace == "ozon" and (not self.client_id or self.business_id is not None)) or (
            self.marketplace == "yandex" and (not self.business_id or self.client_id is not None)
        ):
            raise ValueError("Укажите Client-Id для Ozon или Business ID для Яндекс Маркета")
        return self


class UpdateAccount(Revision):
    title: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None


class ActiveAccount(Revision):
    active: StrictBool


def _done(notice: str):
    return RedirectResponse("/settings?tab=accounts&notice=" + notice, 303)


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v2/accounts")
    def accounts(request: Request):
        saved = manager(request)
        return [asdict(a) for a in request.app.state.connections.list_accounts(saved.user)]

    @router.post("/api/v2/accounts", status_code=201)
    def create(request: Request, payload: CreateAccount):
        saved = manager(request, request.headers.get("x-csrf-token", ""))
        account_id = request.app.state.connections.create(
            saved.user,
            payload.title,
            payload.business_id if payload.marketplace == "yandex" else payload.client_id,
            payload.api_key,
            marketplace=payload.marketplace,
        )
        return {"id": str(account_id)}

    @router.patch("/api/v2/accounts/{account_id}")
    def update(request: Request, account_id: UUID, payload: UpdateAccount):
        saved = manager(request, request.headers.get("x-csrf-token", ""))
        request.app.state.connections.update(
            saved.user, account_id, payload.revision, payload.title, payload.api_key
        )
        return {"status": "saved"}

    @router.post("/api/v2/accounts/{account_id}/active")
    def active(request: Request, account_id: UUID, payload: ActiveAccount):
        saved = manager(request, request.headers.get("x-csrf-token", ""))
        request.app.state.connections.set_active(saved.user, account_id, payload.revision, payload.active)
        return {"status": "saved"}

    @router.post("/api/v2/accounts/{account_id}/check")
    def check(request: Request, account_id: UUID, payload: Revision):
        saved = manager(request, request.headers.get("x-csrf-token", ""))
        results = request.app.state.connections.verify(saved.user, account_id, payload.revision)
        return {"results": results}

    @router.post("/settings/accounts")
    def create_form(
        request: Request,
        csrf: Annotated[str, Form()],
        title: Annotated[str, Form(max_length=200)],
        client_id: Annotated[str, Form(max_length=32)],
        api_key: Annotated[str, Form()],
        marketplace: Annotated[Literal["ozon", "yandex"], Form()] = "ozon",
    ):
        saved = manager(request, csrf)
        try:
            request.app.state.connections.create(
                saved.user, title, client_id, SecretStr(api_key), marketplace=marketplace
            )
        except DomainError as exc:
            return settings_page(
                request,
                saved,
                "accounts",
                error=str(exc),
                form={"title": title, "client_id": client_id, "marketplace": marketplace},
                status=400,
            )
        return _done("saved")

    @router.post("/settings/accounts/{account_id}/edit")
    def edit_form(
        request: Request,
        account_id: UUID,
        csrf: Annotated[str, Form()],
        revision: Annotated[int, Form(ge=0)],
        title: Annotated[str, Form(max_length=200)],
        api_key: Annotated[str, Form()] = "",
    ):
        saved = manager(request, csrf)
        try:
            request.app.state.connections.update(saved.user, account_id, revision, title, SecretStr(api_key))
        except DomainError as exc:
            return settings_page(request, saved, "accounts", error=str(exc), status=400)
        return _done("updated")

    @router.post("/settings/accounts/{account_id}/active")
    def active_form(
        request: Request,
        account_id: UUID,
        csrf: Annotated[str, Form()],
        revision: Annotated[int, Form(ge=0)],
        active: Annotated[bool, Form()],
    ):
        saved = manager(request, csrf)
        request.app.state.connections.set_active(saved.user, account_id, revision, active)
        return _done("active")

    @router.post("/settings/accounts/{account_id}/check")
    def check_form(
        request: Request, account_id: UUID, csrf: Annotated[str, Form()], revision: Annotated[int, Form(ge=0)]
    ):
        saved = manager(request, csrf)
        request.app.state.connections.verify(saved.user, account_id, revision)
        return _done("checked")

    return router
