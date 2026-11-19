import gzip
import os
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

# BINGUS_API_KEYS=nome:chave,outro:chave. Vazio: nenhum worker entra, mas seed e rank rodam.
KEYS = {
    key: name
    for name, key in (
        pair.split(":", 1) for pair in os.environ.get("BINGUS_API_KEYS", "").split(",") if pair
    )
}


# Chaves da busca pública (/search, /page, /mcp): BINGUS_SEARCH_KEYS=chave1,chave2. Vazio deixa
# tudo aberto, que é o certo enquanto só a WireGuard alcança a API.
SEARCH_KEYS = {k for k in os.environ.get("BINGUS_SEARCH_KEYS", "").split(",") if k}


def search_key_ok(authorization: str, key: str | None) -> bool:
    return not SEARCH_KEYS or (authorization.removeprefix("Bearer ") or key or "") in SEARCH_KEYS


def search_key(request: Request) -> None:
    """Chave por header Authorization: Bearer ou por ?key=, para clientes que só aceitam URL."""
    if not search_key_ok(request.headers.get("authorization", ""), request.query_params.get("key")):
        raise HTTPException(401, "Chave inválida.")


class SearchKeyGate:
    """O mesmo controle na frente de uma app ASGI montada, o MCP."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            query = dict(parse_qsl(scope.get("query_string", b"").decode()))
            if not search_key_ok(headers.get("authorization", ""), query.get("key")):
                await JSONResponse({"detail": "Chave inválida."}, 401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
