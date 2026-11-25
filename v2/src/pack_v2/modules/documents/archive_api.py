from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from pack_v2.web.deps import check_csrf, current
from pack_v2.web.query import msk
from pack_v2.web.views import render


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=0, strict=True)


def build_router():
    router = APIRouter()

    @router.get("/reports/stickers")
    def page(
        request: Request,
        search: UUID | None = None,
        cursor: str | None = Query(default=None, max_length=4096),
    ):
        saved = current(request)
        saved.user.require("stickers")
        archive = request.app.state.sticker_archive
        result = archive.listing(saved, search_id=search, cursor=cursor)
        accounts = request.app.state.connections.account_labels(saved.user, section="stickers")
        next_url = (
            "/reports/stickers?"
            + urlencode(
                {"search": str(search), "cursor": result["next_cursor"]}
                if search
                else {"cursor": result["next_cursor"]}
            )
            if result["next_cursor"]
            else None
        )
        return render(
            request,
            "stickers.html",
            saved=saved,
            title="Стикеры",
            active_tab="stickers",
            result=result,
            accounts=accounts,
            account_names={a.id: a.title for a in accounts},
            account_markets={
                a.id: "Яндекс Маркет" if a.marketplace == "yandex" else "Ozon" for a in accounts
            },
            next_url=next_url,
            msk=msk,
        )

    @router.post("/reports/stickers/search")
    def search(
        request: Request,
        csrf: Annotated[str, Form()],
        account_id: Annotated[str, Form(max_length=36)] = "",
        since: Annotated[str, Form(max_length=10)] = "",
        until: Annotated[str, Form(max_length=10)] = "",
        q: Annotated[str, Form(max_length=4096)] = "",
    ):
        saved = current(request)
        check_csrf(saved, csrf)
        search_id = request.app.state.sticker_archive.search(saved, account_id, since, until, q)
        return RedirectResponse("/reports/stickers?" + urlencode({"search": str(search_id)}), 303)

    @router.get("/api/v2/stickers/{label_id}/marking")
    def marking(request: Request, label_id: UUID):
        return request.app.state.sticker_archive.marking(current(request).user, label_id)

    @router.post("/api/v2/stickers/{label_id}/marking/{assignment_id}/unbind")
    def unbind(request: Request, label_id: UUID, assignment_id: UUID, payload: Release):
        saved = current(request)
        check_csrf(saved, request.headers.get("x-csrf-token", ""))
        return request.app.state.sticker_archive.release(
            saved.user, label_id, assignment_id, payload.revision
        )

    @router.get("/api/v2/stickers/{label_id}/pdf")
    def pdf(
        request: Request,
        label_id: UUID,
        download: bool = False,
        generation: int | None = Query(default=None, ge=1),
    ):
        content, filename = request.app.state.sticker_archive.file(
            current(request).user, label_id, generation
        )
        return Response(
            content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    return router
