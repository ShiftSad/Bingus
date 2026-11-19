import datetime as dt
import time

import msgpack
from fastapi import APIRouter, Depends, Request

from bingus.api import db, metrics
from bingus.api.deps import worker

router = APIRouter(prefix="/embed")

LEASE = dt.timedelta(minutes=15)

# Fila por prioridade no índice chunks_pending. Texto igual em várias páginas é embedado uma
# vez só: a página de menor id leva o chunk, as outras esperam e recebem o vetor em COPY_TO_TWINS.
LEASE_CHUNKS = """
WITH picked AS (
    SELECT c.page_id, c.seq FROM chunks c
    WHERE c.embedding IS NULL AND (c.leased_until IS NULL OR c.leased_until < now())
      AND NOT EXISTS (
          SELECT 1 FROM pages p
          JOIN pages o ON o.content_hash = p.content_hash AND o.id < p.id
          JOIN chunks oc ON oc.page_id = o.id AND oc.seq = c.seq AND oc.embedding IS NULL
          WHERE p.id = c.page_id)
    ORDER BY c.priority DESC LIMIT $1
    FOR UPDATE OF c SKIP LOCKED
)
UPDATE chunks c SET leased_until = now() + $2
FROM picked JOIN texts t ON t.page_id = picked.page_id
WHERE c.page_id = picked.page_id AND c.seq = picked.seq
RETURNING c.page_id, c.seq, substr(t.text, c.start_ch + 1, c.end_ch - c.start_ch) AS text
"""

SAVE_EMBEDDING = """
UPDATE chunks
SET embedding = quantize_to_rabitq8($3::vector), embedded_at = now(), leased_until = NULL
WHERE page_id = $1 AND seq = $2
"""

# Gêmeas que esperavam pelo mesmo texto. Sem embedded_at: o painel conta só o trabalho da GPU.
COPY_TO_TWINS = """
UPDATE chunks t SET embedding = s.embedding, leased_until = NULL
FROM chunks s
JOIN pages a ON a.id = s.page_id
JOIN pages b ON b.content_hash = a.content_hash AND b.id <> a.id
WHERE s.page_id = $1 AND s.seq = $2 AND t.page_id = b.id AND t.seq = s.seq AND t.embedding IS NULL
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
        await conn.executemany(COPY_TO_TWINS, [(page_id, seq) for page_id, seq, _ in rows])
        ms = int((time.monotonic() - started) * 1000)
        await metrics.record_batch(conn, name, ms, chunks_embedded=len(rows))
    return {"ok": True}


def vector(raw: bytes) -> str:
    """int8 bytes -> pgvector literal."""
    return "[" + ",".join(map(str, memoryview(raw).cast("b").tolist())) + "]"
