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


GPU_QUERY = "utilization.gpu,memory.used,power.draw"


async def gpu_sample() -> dict[str, float]:
    """Use, VRAM and power of the first GPU from nvidia-smi; empty when there is none."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            f"--query-gpu={GPU_QUERY}",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
        util, mem, watts = (float(v) for v in out.decode().split("\n")[0].split(","))
    except Exception:
        return {}
    return {"gpu": util, "gpu_mem": int(mem * 2**20), "gpu_watts": watts}


class Stats:
    """Counters for the current sample window, pushed to the API every few seconds."""

    def __init__(self, gpu: bool = False) -> None:
        self.gpu = gpu  # só o embed: os fetch workers nativos veriam a GPU da máquina
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
                    **(await gpu_sample() if self.gpu else {}),
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
