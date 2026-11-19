import asyncio
import datetime as dt
import json
import time
from collections import Counter

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from bingus.api import db
from bingus.api.deps import GzipRoute, worker

router = APIRouter(route_class=GzipRoute)

RETENTION = dt.timedelta(days=90)

# Contadores do GET /metrics, em memória desde que a API subiu: um restart os zera e o rate() do
# Prometheus entende. Somar a tabela batches não serve: a limpeza dos 90 dias faria o total cair,
# e o Prometheus leria a queda como restart, com um pico do tamanho do total.
totals: Counter[tuple[str, str]] = Counter()  # (métrica, rótulos) -> soma
latest: dict[str, tuple[float, "Sample"]] = {}  # worker -> (hora, última amostra)
GAUGES = {  # métrica -> campo da última amostra
    "bingus_worker_cpu_cores": "cpu",
    "bingus_worker_gpu_percent": "gpu",
    "bingus_worker_gpu_memory_bytes": "gpu_mem",
    "bingus_worker_gpu_watts": "gpu_watts",
}

HELP = {
    "bingus_pages": ("gauge", "Páginas buscadas, inclusive falhas e outros idiomas"),
    "bingus_pages_with_text": ("gauge", "Páginas com texto extraído, estimativa do ANALYZE"),
    "bingus_frontier_urls": ("gauge", "URLs descobertas e nunca buscadas"),
    "bingus_hosts": ("gauge", "Hosts conhecidos"),
    "bingus_hosts_due": ("gauge", "Hosts com trabalho vencido, amostra do último minuto"),
    "bingus_hosts_leased": ("gauge", "Hosts com fetch workers, amostra do último minuto"),
    "bingus_chunks_pending": ("gauge", "Chunks na fila do embed, estimativa do ANALYZE"),
    "bingus_chunks_with_embedding": ("gauge", "Chunks com embedding, estimativa do ANALYZE"),
    "bingus_database_bytes": ("gauge", "Tamanho do banco"),
    "bingus_table_bytes": ("gauge", "Tamanho da tabela com índices e TOAST"),
    "bingus_ingested_pages_total": ("counter", "Resultados de página ingeridos, por desfecho"),
    "bingus_frontier_added_total": ("counter", "URLs novas na frontier"),
    "bingus_chunks_queued_total": ("counter", "Chunks enfileirados para o embed"),
    "bingus_chunks_embedded_total": ("counter", "Chunks que receberam embedding"),
    "bingus_batches_total": ("counter", "Lotes de resultados ingeridos"),
    "bingus_batch_seconds_total": ("counter", "Tempo gasto ingerindo lotes"),
    "bingus_worker_requests_total": ("counter", "Requests HTTP do worker"),
    "bingus_worker_bytes_total": ("counter", "Bytes baixados pelo worker"),
    "bingus_worker_pages_total": ("counter", "Resultados prontos no worker"),
    "bingus_worker_errors_total": ("counter", "Erros do worker por tipo"),
    "bingus_worker_cpu_cores": ("gauge", "Núcleos usados pelo worker na última amostra"),
    "bingus_worker_gpu_percent": ("gauge", "Uso da GPU do embed na última amostra"),
    "bingus_worker_gpu_memory_bytes": ("gauge", "VRAM em uso na última amostra"),
    "bingus_worker_gpu_watts": ("gauge", "Potência da GPU na última amostra"),
}

# n_live_tup acompanha inserts e deletes na hora, reltuples só muda no ANALYZE. Os índices
# parciais só têm reltuples, e os dois são estimados pelo mesmo ANALYZE de chunks.
STATE = """
WITH live AS (
    SELECT relname, n_live_tup AS n FROM pg_stat_user_tables WHERE schemaname = 'public'
)
SELECT
    (SELECT n FROM live WHERE relname = 'pages') AS pages,
    (SELECT round(n * (1 - null_frac))::bigint FROM live, pg_stats
     WHERE relname = 'pages' AND schemaname = 'public' AND tablename = 'pages'
       AND attname = 'text') AS pages_with_text,
    (SELECT n FROM live WHERE relname = 'frontier') AS frontier_urls,
    (SELECT n FROM live WHERE relname = 'hosts') AS hosts,
    (SELECT hosts_due FROM api_samples ORDER BY at DESC LIMIT 1) AS hosts_due,
    (SELECT hosts_leased FROM api_samples ORDER BY at DESC LIMIT 1) AS hosts_leased,
    (SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE relname = 'chunks_pending')
        AS chunks_pending,
    (SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE relname = 'chunks_embedded_at')
        AS chunks_with_embedding,
    pg_database_size(current_database()) AS database_bytes
"""

TABLE_BYTES = """
SELECT relname, pg_total_relation_size(oid) FROM pg_class
WHERE relkind = 'r' AND relname IN ('pages', 'chunks', 'frontier', 'hosts')
"""


class Sample(BaseModel):
    at: dt.datetime
    requests: int = 0
    bytes: int = 0
    pages: int = 0
    errors: dict[str, int] = {}
    cpu: float = 0
    gpu: float | None = None  # só o embed manda: % de uso, bytes de VRAM, watts
    gpu_mem: int | None = None
    gpu_watts: float | None = None


class Samples(BaseModel):
    instance: str = ""  # máquina; várias podem usar a mesma chave
    samples: list[Sample]


SAVE_SAMPLE = """
INSERT INTO worker_samples
    (worker, at, requests, bytes, pages, errors, cpu, gpu, gpu_mem, gpu_watts)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10) ON CONFLICT DO NOTHING
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
        (
            name,
            s.at,
            s.requests,
            s.bytes,
            s.pages,
            json.dumps(s.errors),
            s.cpu,
            s.gpu,
            s.gpu_mem,
            s.gpu_watts,
        )
        for s in body.samples
    ]
    await db.pool().executemany(SAVE_SAMPLE, rows)
    for s in body.samples:
        count("bingus_worker_requests_total", s.requests, worker=name)
        count("bingus_worker_bytes_total", s.bytes, worker=name)
        count("bingus_worker_pages_total", s.pages, worker=name)
        for kind, n in s.errors.items():
            count("bingus_worker_errors_total", n, worker=name, kind=kind)
    if body.samples:
        latest[name] = (time.time(), body.samples[-1])
    return {"ok": True}


@router.get("/metrics", dependencies=[Depends(worker)])
async def prometheus() -> PlainTextResponse:
    """Prometheus text format: state from the database plus the counters above."""
    async with db.pool().acquire() as conn:
        state = await conn.fetchrow(STATE) or {}
        tables = await conn.fetch(TABLE_BYTES)
    series = [(f"bingus_{k}", "", v) for k, v in state.items() if v is not None]
    series += [("bingus_table_bytes", labels(table=t), n) for t, n in tables]
    series += [(metric, lbl, n) for (metric, lbl), n in totals.items()]
    now = time.time()
    series += [
        (metric, labels(worker=w), value)
        for w, (at, sample) in latest.items()
        if now - at < 120  # worker parado some do gráfico em vez de ficar no último valor
        for metric, field in GAUGES.items()
        if (value := getattr(sample, field)) is not None
    ]
    out, last = [], ""
    for metric, lbl, value in sorted(series):
        if metric != last:
            kind, text = HELP[metric]
            out += [f"# HELP {metric} {text}", f"# TYPE {metric} {kind}"]
            last = metric
        out.append(f"{metric}{lbl} {value}")
    return PlainTextResponse("\n".join(out) + "\n", media_type="text/plain; version=0.0.4")


def labels(**pairs: str) -> str:
    def escape(v: str) -> str:
        return v.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")

    return "{" + ",".join(f'{k}="{escape(v)}"' for k, v in pairs.items()) + "}"


def count(metric: str, n: float, **pairs: str) -> None:
    if n:
        totals[metric, labels(**pairs)] += n


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
    for outcome in ("new", "changed", "unchanged", "failed", "foreign"):
        count("bingus_ingested_pages_total", o[outcome], worker=name, outcome=outcome)
    count("bingus_frontier_added_total", frontier_added, worker=name)
    count("bingus_chunks_queued_total", chunks_queued, worker=name)
    count("bingus_chunks_embedded_total", chunks_embedded, worker=name)
    count("bingus_batches_total", 1, worker=name)
    count("bingus_batch_seconds_total", ms / 1000, worker=name)


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
