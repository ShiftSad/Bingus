import gzip
import json
from typing import Any

import httpx
import msgpack


class Api:
    """HTTP client used by workers. The API key identifies the worker."""

    def __init__(self, url: str, key: str):
        self.http = httpx.AsyncClient(
            base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=300
        )

    async def get(self, path: str, **params: str | int) -> Any:
        r = await self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def post_json(self, path: str, data: object) -> Any:
        r = await self.http.post(
            path,
            content=gzip.compress(json.dumps(data).encode()),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        r.raise_for_status()
        return r.json()

    async def post_msgpack(self, path: str, data: object) -> Any:
        r = await self.http.post(
            path, content=msgpack.packb(data), headers={"Content-Type": "application/msgpack"}
        )
        r.raise_for_status()
        return r.json()
