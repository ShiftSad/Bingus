import asyncio
import datetime as dt
import logging
import os
import socket
import time
from collections import Counter
from typing import Any

from bingus.common.client import Api

log = logging.getLogger(__name__)

# Várias máquinas usam a mesma chave; o nome separa as séries no dashboard.
INSTANCE = os.environ.get("BINGUS_NAME", socket.gethostname())


class Stats:
    """Counters for the current sample window, pushed to the API every few seconds."""

    def __init__(self) -> None:
        self.requests = 0
        self.bytes = 0
        self.pages = 0
        self.errors: Counter[str] = Counter()
        self.pending: list[dict[str, Any]] = []

    def error(self, kind: str) -> None:
        self.errors[kind] += 1

    async def run(self, api: Api, every: float = 10) -> None:
        cpu = time.process_time()
        while True:
            await asyncio.sleep(every)
            now = time.process_time()
            self.pending.append(
                {
                    "at": dt.datetime.now(dt.UTC).isoformat(),
                    "requests": self.requests,
                    "bytes": self.bytes,
                    "pages": self.pages,
                    "errors": dict(self.errors),
                    "cpu": (now - cpu) / every,
                }
            )
            self.requests = self.bytes = self.pages = 0
            self.errors.clear()
            cpu = now
            try:
                await api.post_json("/metrics", {"instance": INSTANCE, "samples": self.pending})
                self.pending.clear()
            except Exception as e:
                log.warning("metrics push failed: %s", e)
                del self.pending[:-360]  # keep the last hour and retry next time
