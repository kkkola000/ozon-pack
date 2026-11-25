from starlette.responses import PlainTextResponse


class AuthBodyLimit:
    """Bound auth bodies before form/JSON parsing, including chunked requests."""

    def __init__(self, app, limit: int = 16_384):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        protected = scope.get("path") in {"/login", "/logout", "/api/v2/users"}
        protected |= scope.get("path", "").startswith(
            (
                "/api/v2/stickers",
                "/api/v2/printing",
                "/reports/stickers/search",
                "/api/v2/assembly",
                "/api/v2/accounts",
                "/settings/accounts",
                "/api/v2/catalog",
                "/api/v2/orders/sync",
                "/orders/sync",
                "/products/sync",
            )
        )
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PATCH"} or not protected:
            return await self.app(scope, receive, send)
        # A valid 1000-unit layout contains about 40 KiB of UUIDs; other commands stay at 16 KiB.
        layout = scope.get("path", "").startswith("/api/v2/assembly/") and scope.get("path", "").endswith(
            "/layout"
        )
        limit = 65_536 if layout else self.limit
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > limit:
                response = PlainTextResponse("Слишком большой запрос", status_code=413)
                return await response(scope, receive, send)
            chunks.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(chunks)

        async def replay():
            try:
                return next(iterator)
            except StopIteration:
                return await receive()

        return await self.app(scope, replay, send)
