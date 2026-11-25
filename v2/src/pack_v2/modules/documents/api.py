from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response

from pack_v2.web.deps import current


def build_router():
    router = APIRouter()

    @router.get("/api/v2/documents/labels/{label_id}/{generation}/pdf")
    def pdf(request: Request, label_id: UUID, generation: int, download: bool = False):
        saved = current(request)
        content, filename = request.app.state.documents.file(saved.user, label_id, generation)
        return Response(
            content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    return router
