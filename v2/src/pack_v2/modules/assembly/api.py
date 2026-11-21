from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from pack_v2.web.deps import check_csrf, current
from pack_v2.web.query import AccountFilter, msk
from pack_v2.web.views import render


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    command_id: UUID


class Open(Command):
    account_id: UUID | None = None
    shipment_id: UUID | None = None
    code: str | None = Field(default=None, max_length=4096)


class SessionCommand(Command):
    lease_token: UUID
    revision: int = Field(ge=0, strict=True)


class Scan(SessionCommand):
    code: str = Field(min_length=1, max_length=4096)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    lease_token: UUID


def _actor(request, *, write=False):
    saved = current(request)
    saved.user.require("pack")
    if write:
        check_csrf(saved, request.headers.get("x-csrf-token", ""))
    return saved


def _response(result):
    return JSONResponse(
        jsonable_encoder(result),
        status_code=202 if result.get("pending") else 200 if result.get("ok") else 409,
    )


def build_router():
    router = APIRouter()

    @router.get("/pack")
    def page(request: Request, account_id: AccountFilter = None, shipment_id: UUID | None = None):
        saved = _actor(request)
        service = request.app.state.assembly
        return render(
            request,
            "assembly.html",
            saved=saved,
            title="Сборка",
            active_tab="pack",
            queue=service.queue(saved.user, account_id),
            session=service.current(saved.user),
            accounts=request.app.state.connections.account_labels(saved.user, section="pack"),
            selected_account=account_id,
            open_shipment=str(shipment_id) if shipment_id else None,
            msk=msk,
        )

    @router.get("/api/v2/assembly/current")
    def active(request: Request):
        return {"session": request.app.state.assembly.current(_actor(request).user)}

    @router.get("/api/v2/assembly/commands/{command_id}")
    def result(request: Request, command_id: UUID):
        return request.app.state.assembly.command(_actor(request).user, command_id)

    @router.post("/api/v2/assembly/open")
    def open_session(request: Request, payload: Open):
        saved = _actor(request, write=True)
        return _response(
            request.app.state.assembly.open(
                saved.user,
                payload.command_id,
                shipment_id=payload.shipment_id,
                code=payload.code,
                account_id=payload.account_id,
            )
        )

    @router.get("/api/v2/assembly/{session_id}")
    def session(request: Request, session_id: UUID):
        return {"session": request.app.state.assembly.session(_actor(request).user, session_id)}

    @router.post("/api/v2/assembly/{session_id}/scan")
    def scan(request: Request, session_id: UUID, payload: Scan):
        saved = _actor(request, write=True)
        return _response(
            request.app.state.assembly.scan(
                saved.user,
                payload.command_id,
                session_id,
                payload.lease_token,
                payload.revision,
                payload.code,
            )
        )

    @router.post("/api/v2/assembly/{session_id}/activate")
    def activate(request: Request, session_id: UUID, payload: SessionCommand):
        saved = _actor(request, write=True)
        return _response(
            request.app.state.assembly.activate(
                saved.user, payload.command_id, session_id, payload.lease_token, payload.revision
            )
        )

    @router.post("/api/v2/assembly/{session_id}/verify")
    def verify(request: Request, session_id: UUID, payload: SessionCommand):
        saved = _actor(request, write=True)
        return _response(
            request.app.state.assembly.verify(
                saved.user, payload.command_id, session_id, payload.lease_token, payload.revision
            )
        )

    @router.post("/api/v2/assembly/{session_id}/cancel")
    def cancel(request: Request, session_id: UUID, payload: SessionCommand):
        saved = _actor(request, write=True)
        return _response(
            request.app.state.assembly.cancel(
                saved.user, payload.command_id, session_id, payload.lease_token, payload.revision
            )
        )

    @router.post("/api/v2/assembly/{session_id}/retry")
    def retry(request: Request, session_id: UUID, payload: SessionCommand):
        saved = _actor(request, write=True)
        return _response(
            request.app.state.assembly.retry(
                saved.user, payload.command_id, session_id, payload.lease_token, payload.revision
            )
        )

    @router.post("/api/v2/assembly/{session_id}/heartbeat")
    def heartbeat(request: Request, session_id: UUID, payload: Heartbeat):
        saved = _actor(request, write=True)
        return _response(request.app.state.assembly.heartbeat(saved.user, session_id, payload.lease_token))

    return router
