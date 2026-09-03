import asyncio
import datetime as dt
import json
from collections import Counter

import asyncpg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from bingus.api import db
from bingus.api.deps import GzipRoute, worker

router = APIRouter(route_class=GzipRoute)

RETENTION = dt.timedelta(days=90)


class Sample(BaseModel):
    at: dt.datetime
    requests: int = 0
    bytes: int = 0
    pages: int = 0
    errors: dict[str, int] = {}
    cpu: float = 0


class Samples(BaseModel):
    instance: str = ""  # máquina; várias podem usar a mesma chave
    samples: list[Sample]


SAVE_SAMPLE = """
INSERT INTO worker_samples (worker, at, requests, bytes, pages, errors, cpu)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) ON CONFLICT DO NOTHING
"""

SAVE_BATCH = """
INSERT INTO batches (worker, ms, pages_new, pages_changed, pages_unchanged, pages_failed,
                     pages_foreign, frontier_added, chunks_queued, chunks_embedded)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) ON CONFLICT DO NOTHING
"""

SAVE_API_SAMPLE = """
INSERT INTO api_samples (frontier, pages, chunks_pending, hosts, hosts_due, hosts_leased)
SELECT
    greatest((SELECT reltuples FROM pg_class WHERE relname = 'frontier'), 0),
    greatest((SELECT reltuples FROM pg_class WHERE relname = 'pages'), 0),
    greatest((SELECT reltuples FROM pg_class WHERE relname = 'chunks_pending'), 0),
    (SELECT count(*) FROM hosts),
    (SELECT count(*) FROM hosts WHERE NOT blocked AND next_due <= now()),
    (SELECT count(*) FROM hosts WHERE leased_until > now())
"""

PRUNE = [
    f"DELETE FROM {table} WHERE at < now() - $1::interval"
    for table in ("worker_samples", "batches", "api_samples")
]


@router.post("/metrics")
async def ingest(body: Samples, name: str = Depends(worker)):
    if body.instance:
        name = f"{name}/{body.instance}"
    rows = [
        (name, s.at, s.requests, s.bytes, s.pages, json.dumps(s.errors), s.cpu)
        for s in body.samples
    ]
    await db.pool().executemany(SAVE_SAMPLE, rows)
    return {"ok": True}


async def record_batch(
    conn: asyncpg.pool.PoolConnectionProxy,
    name: str,
    ms: int,
    outcomes: Counter[str] | None = None,
    frontier_added: int = 0,
    chunks_queued: int = 0,
    chunks_embedded: int = 0,
) -> None:
    o = outcomes or Counter()
    await conn.execute(
        SAVE_BATCH,
        name,
        ms,
        o["new"],
        o["changed"],
        o["unchanged"],
        o["failed"],
        o["foreign"],
        frontier_added,
        chunks_queued,
        chunks_embedded,
    )


async def sample_loop(every: float = 60) -> None:
    """Snapshot the global state every minute and prune old telemetry once a day."""
    tick = 0
    while True:
        await db.pool().execute(SAVE_API_SAMPLE)
        if tick % 1440 == 0:
            for sql in PRUNE:
                await db.pool().execute(sql, RETENTION)
        tick += 1
        await asyncio.sleep(every)
