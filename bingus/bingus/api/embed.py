import datetime as dt
import time

import msgpack
from fastapi import APIRouter, Depends, Request

from bingus.api import db, metrics
from bingus.api.deps import worker

router = APIRouter(prefix="/embed")

LEASE = dt.timedelta(minutes=15)

# Páginas de maior PageRank primeiro, deixando a Wikipédia atrás dos demais hosts.
LEASE_CHUNKS = """
WITH non_wikipedia AS MATERIALIZED (
    SELECT p.id, p.rank FROM pages p
    WHERE p.host <> 'pt.wikipedia.org'
      AND EXISTS (SELECT 1 FROM chunks c WHERE c.page_id = p.id AND c.embedding IS NULL
                  AND (c.leased_until IS NULL OR c.leased_until < now()))
    ORDER BY p.rank DESC LIMIT $1
), wikipedia AS (
    SELECT p.id, p.rank FROM pages p
    WHERE p.host = 'pt.wikipedia.org'
      AND EXISTS (SELECT 1 FROM chunks c WHERE c.page_id = p.id AND c.embedding IS NULL
                  AND (c.leased_until IS NULL OR c.leased_until < now()))
    ORDER BY p.rank DESC LIMIT greatest($1 - (SELECT count(*) FROM non_wikipedia), 0)
), due AS (
    SELECT id, rank, false AS is_wikipedia FROM non_wikipedia
    UNION ALL
    SELECT id, rank, true AS is_wikipedia FROM wikipedia
), picked AS (
    SELECT c.page_id, c.seq FROM chunks c JOIN due d ON d.id = c.page_id
    WHERE c.embedding IS NULL AND (c.leased_until IS NULL OR c.leased_until < now())
    ORDER BY d.is_wikipedia, d.rank DESC LIMIT $1
    FOR UPDATE OF c SKIP LOCKED
)
UPDATE chunks c SET leased_until = now() + $2
FROM picked JOIN pages p ON p.id = picked.page_id
WHERE c.page_id = picked.page_id AND c.seq = picked.seq
RETURNING c.page_id, c.seq, substr(p.text, c.start_ch + 1, c.end_ch - c.start_ch) AS text
"""

SAVE_EMBEDDING = """
UPDATE chunks
SET embedding = quantize_to_rabitq8($3::vector), embedded_at = now(), leased_until = NULL
WHERE page_id = $1 AND seq = $2
"""


@router.post("/batch")
async def batch(size: int = 64, name: str = Depends(worker)):
    """Lease chunks without embedding and hand their text to the worker."""
    rows = await db.pool().fetch(LEASE_CHUNKS, size, LEASE)
    return {"chunks": [dict(r) for r in rows]}


@router.post("/results")
async def results(request: Request, name: str = Depends(worker)):
    """msgpack body: {"chunks": [{"page_id", "seq", "embedding": 512 int8 bytes}]}"""
    started = time.monotonic()
    data = msgpack.unpackb(await request.body())
    rows = [(c["page_id"], c["seq"], vector(c["embedding"])) for c in data["chunks"]]
    async with db.pool().acquire() as conn, conn.transaction():
        await conn.executemany(SAVE_EMBEDDING, rows)
        ms = int((time.monotonic() - started) * 1000)
        await metrics.record_batch(conn, name, ms, chunks_embedded=len(rows))
    return {"ok": True}


def vector(raw: bytes) -> str:
    """int8 bytes -> pgvector literal."""
    return "[" + ",".join(map(str, memoryview(raw).cast("b").tolist())) + "]"
