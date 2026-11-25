from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from pack_v2.web.deps import check_csrf, current


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Profile(Strict):
    kind: Literal["ozon:label", "yandex:label", "marking:duplicate"]
    size: Literal["58x40", "75x120", "100x150", "a4", "custom"] = "58x40"
    printer: str = Field(default="", max_length=200)
    orientation: Literal["", "portrait", "landscape"] = ""
    width: float = Field(default=58, ge=30, le=500, allow_inf_nan=False)
    height: float = Field(default=40, ge=30, le=500, allow_inf_nan=False)
    gap: float = Field(default=0, ge=0, le=20, allow_inf_nan=False)
    x: float = Field(default=0, ge=-30, le=30, allow_inf_nan=False)
    y: float = Field(default=0, ge=-30, le=30, allow_inf_nan=False)


class Setup(Strict):
    revision: int = Field(ge=0, strict=True)
    product_barcodes: bool = Field(strict=True)
    marking_duplicates: bool = Field(strict=True)
    profiles: list[Profile] = Field(min_length=2, max_length=9)


class Command(Strict):
    request_id: UUID


class Label(Command):
    label_id: UUID
    generation: int = Field(ge=1, strict=True)
    session_id: UUID | None = None


class Assignment(Strict):
    id: UUID
    revision: int = Field(ge=0, strict=True)


class Marks(Command):
    label_id: UUID
    assignments: list[Assignment] = Field(min_length=1, max_length=100)


class Sample(Command):
    profile: Profile


class Token(Strict):
    dispatch_token: UUID


class Result(Strict):
    delivery_token: UUID
    state: Literal["submitted", "browser", "uncertain", "failed"]


def build_router():
    router = APIRouter(prefix="/api/v2/printing")

    def actor(request, write=False):
        saved = current(request)
        saved.user.require("printing")
        if write:
            check_csrf(saved, request.headers.get("x-csrf-token", ""))
        return saved.user

    @router.get("/settings")
    def setup(request: Request):
        return request.app.state.printing.settings(actor(request))

    @router.post("/settings")
    def save(request: Request, payload: Setup):
        return request.app.state.printing.save(
            actor(request, True),
            payload.revision,
            payload.product_barcodes,
            payload.marking_duplicates,
            [r.model_dump() for r in payload.profiles],
        )

    @router.post("/assembly/{session_id}")
    def plan(request: Request, session_id: UUID):
        return request.app.state.printing.plan(actor(request, True), session_id)

    @router.post("/label")
    def label(request: Request, payload: Label):
        return request.app.state.printing.label(
            actor(request, True),
            payload.request_id,
            payload.label_id,
            payload.generation,
            session_id=payload.session_id,
        )

    @router.post("/marking")
    def marking(request: Request, payload: Marks):
        return request.app.state.printing.marks(
            actor(request, True),
            payload.request_id,
            payload.label_id,
            [{"id": str(r.id), "revision": r.revision} for r in payload.assignments],
        )

    @router.post("/test")
    def test(request: Request, payload: Sample):
        return request.app.state.printing.test(
            actor(request, True), payload.request_id, payload.profile.model_dump()
        )

    @router.get("/jobs/{job_id}/pdf")
    def pdf(request: Request, job_id: UUID):
        data = request.app.state.printing.file(actor(request), job_id)
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="pack-print-{job_id}.pdf"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/jobs/{job_id}/start")
    def start(request: Request, job_id: UUID, payload: Token):
        return request.app.state.printing.start(actor(request, True), job_id, payload.dispatch_token)

    @router.post("/jobs/{job_id}/finish")
    def finish(request: Request, job_id: UUID, payload: Result):
        return request.app.state.printing.finish(
            actor(request, True), job_id, payload.delivery_token, payload.state
        )

    @router.get("/jobs/{job_id}")
    def status(request: Request, job_id: UUID):
        return request.app.state.printing.status(actor(request), job_id)

    @router.get("/qz/certificate")
    def certificate(request: Request):
        return PlainTextResponse(request.app.state.printing.certificate(actor(request)))

    @router.post("/qz/sign")
    async def sign(request: Request):
        user = actor(request, True)
        value = (await request.body()).decode("ascii", errors="replace")
        return PlainTextResponse(request.app.state.printing.sign(user, value))

    return router
