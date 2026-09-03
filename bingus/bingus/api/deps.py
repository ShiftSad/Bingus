import gzip
import os
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Header, HTTPException, Request, Response
from fastapi.routing import APIRoute

# BINGUS_API_KEYS=nome:chave,outro:chave. Vazio: nenhum worker entra, mas seed e rank rodam.
KEYS = {
    key: name
    for name, key in (
        pair.split(":", 1) for pair in os.environ.get("BINGUS_API_KEYS", "").split(",") if pair
    )
}


def worker(authorization: str = Header("")) -> str:
    """Name of the worker that owns the API key."""
    name = KEYS.get(authorization.removeprefix("Bearer "))
    if not name:
        raise HTTPException(401)
    return name


class GzipRequest(Request):
    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            body = await super().body()
            if self.headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            self._body = body
        return self._body


class GzipRoute(APIRoute):
    """Route that accepts gzip-compressed request bodies."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            return await handler(GzipRequest(request.scope, request.receive))

        return handle
