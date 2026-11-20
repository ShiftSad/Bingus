import asyncio
import datetime as dt
import logging
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from bingus.api import db, metrics
from bingus.api.deps import GzipRoute, worker
from bingus.common.hashing import content_hash, hamming, url_hash
from bingus.common.urls import host_of

log = logging.getLogger("bingus.fetch")
router = APIRouter(prefix="/fetch", route_class=GzipRoute)

NEW_INTERVAL = dt.timedelta(days=7)
MIN_INTERVAL = dt.timedelta(days=1)
MAX_INTERVAL = dt.timedelta(days=90)
DEAD_INTERVAL = dt.timedelta(days=90)  # redirects e páginas fora do idioma
MAX_DEPTH = 5
MAX_LINKS = 500
MAX_PER_HOST = 100  # hosts grandes recebem até isso por lote
HOST_BUDGET = 60  # segundos de relógio que um host ocupa num lote; o worker para ao estourar
# Orçamento, o download que estava em curso e o próximo envio. Worker que morre perde os hosts por
# no máximo isso; antes a lease crescia com o crawl_delay e chegava a 50 minutos.
LEASE_SECONDS = HOST_BUDGET + 120
MAX_FAILS = 10
REEMBED_BITS = 3
ROBOTS_BLOCKED = 999  # status inventado pelo worker: página morre na hora
# Coleta focada: a lease só entrega hosts com pelo menos essa prioridade, ou semeados à mão. Com 4,
# o foco nerd e as referências (6) e os sites linkados por eles (4); 0 desliga o filtro.
MIN_PRIORITY = int(os.environ.get("BINGUS_MIN_PRIORITY", "0"))


class Chunk(BaseModel):
    start: int
    end: int
    simhash: int


class PageResult(BaseModel):
    url: str
    depth: int = 0
    status: int = 0  # 0 = erro de rede
    final_url: str | None = None  # depois de redirects, já normalizada
    etag: str | None = None
    last_modified: str | None = None
    title: str | None = None
    text: str | None = None  # título na primeira linha; None em hubs, que só têm links
    lang: str | None = None
    published: str | None = None  # data de publicação, YYYY-MM-DD, quando a página diz
    chunks: list[Chunk] = []
    links: list[str] = []  # já normalizadas

    @field_validator("title", "text")
    @classmethod
    def no_nul(cls, v: str | None) -> str | None:
        return v.replace("\x00", "") if v else v  # Postgres recusa \x00 em text


class HostResult(BaseModel):
    robots: str | None = None  # None = não buscou desta vez
    crawl_delay: float = 1.0
    sitemaps: bool = False  # leu os sitemaps nesta rodada

    @field_validator("robots")
    @classmethod
    def no_nul(cls, v: str | None) -> str | None:
        return v.replace("\x00", "") if v else v


class FetchResults(BaseModel):
    pages: list[PageResult]
    hosts: dict[str, HostResult] = {}
    urls: list[str] = []  # achadas em sitemaps


class Release(BaseModel):
    hosts: list[str]


class Seed(BaseModel):
    urls: list[str]
    langs: list[str] | None = None  # idiomas aceitos nos hosts dessas URLs, ex. ["pt", "en"]
    priority: int = 0  # foco do crawl: hosts nerd/tech na frente, ver PRIORITY_DECAY


Links = dict[int, tuple[str, str, int]]  # url_hash -> (url, host, depth)
Conn = asyncpg.pool.PoolConnectionProxy

LEASE_HOSTS = """
WITH picked AS (
    SELECT host FROM hosts
    WHERE NOT blocked AND next_due <= now() AND (leased_until IS NULL OR leased_until < now())
      AND (priority >= $3 OR seeded)
    ORDER BY priority DESC, seeded DESC, (page_count = 0) DESC, (host LIKE '%.br') DESC, next_due
    LIMIT $1
    FOR UPDATE SKIP LOCKED
)
UPDATE hosts h
SET leased_until = now() + make_interval(secs => $2)
FROM picked WHERE h.host = picked.host
RETURNING h.host, h.crawl_delay, h.robots, h.page_count,
    h.page_count < h.max_pages AS accepts_new,
    h.pt_count > 0 AND coalesce(h.sitemaps_at < now() - interval '1 day', true) AS want_sitemaps
"""

SAVE_HOST = """
UPDATE hosts SET robots = coalesce($2, robots), robots_at = coalesce(robots_at, now()),
    crawl_delay = $3, sitemaps_at = CASE WHEN $4 THEN now() ELSE sitemaps_at END
WHERE host = $1
"""

# Três páginas estrangeiras e nenhuma em português: host bloqueado e frontier dele apagada.
FOREIGN_HOST = """
WITH h AS (
    UPDATE hosts SET foreign_count = foreign_count + 1,
        blocked = blocked OR (foreign_count + 1 >= 3 AND pt_count = 0)
    WHERE host = $1 RETURNING blocked
)
DELETE FROM frontier WHERE host = $1 AND (SELECT blocked FROM h)
"""

FRONTIER_URLS = """
SELECT url, depth, NULL::text AS etag, NULL::text AS last_modified
FROM frontier WHERE host = $1 ORDER BY added_at DESC LIMIT $2
"""

DUE_URLS = """
SELECT url, depth, etag, last_modified
FROM pages WHERE host = $1 AND fail_count < 10 AND next_check_at <= now()
ORDER BY next_check_at LIMIT $2
"""

# Quanto maior o host, mais ele espera entre lotes: sqrt(page_count) segundos, dividido pela
# prioridade, para os hosts do foco andarem no ritmo do crawl_delay. Nunca menos que o próprio
# crawl_delay: com Crawl-delay de uma hora, o worker faz um request e devolve o host.
COOLDOWN = "now() + make_interval(secs => greatest(sqrt(page_count) / (1 + priority), crawl_delay))"

RELEASE_HOST = f"""
UPDATE hosts h SET leased_until = NULL, next_due = greatest({COOLDOWN}, coalesce(least(
    CASE WHEN h.page_count < h.max_pages
         THEN (SELECT min(f.added_at) FROM frontier f WHERE f.host = h.host) END,
    (SELECT min(p.next_check_at) FROM pages p WHERE p.host = h.host AND p.fail_count < 10)
), 'infinity'))
WHERE h.host = $1
"""

# Hosts .br aceitam inglês também: site brasileiro publicando em inglês é conteúdo nosso.
INSERT_HOSTS = """
INSERT INTO hosts (host, langs)
SELECT h, CASE WHEN h LIKE '%.br' THEN '{pt,en}' ELSE '{pt}' END::text[]
FROM unnest($1::text[]) AS t(h) ON CONFLICT DO NOTHING
"""

# Link achado num host prioritário: o destino herda a prioridade menos PRIORITY_DECAY. Só para
# esses, porque DO UPDATE trava a linha e hosts populares eram fonte de deadlock.
INSERT_HOSTS_PRIORITY = """
INSERT INTO hosts (host, langs, priority)
SELECT h, CASE WHEN h LIKE '%.br' THEN '{pt,en}' ELSE '{pt}' END::text[], $2
FROM unnest($1::text[]) AS t(h)
ON CONFLICT (host) DO UPDATE SET priority = EXCLUDED.priority
WHERE hosts.priority < EXCLUDED.priority AND hosts.page_count = 0
"""
PRIORITY_DECAY = 2

ENSURE_PAGE = """
INSERT INTO pages (url_hash, url, host, depth) VALUES ($1, $2, $3, $4)
ON CONFLICT (url_hash) DO NOTHING RETURNING id
"""

TOUCH_PAGE = """
UPDATE pages SET status = $2, fail_count = $3, check_interval = $4, next_check_at = now() + $4,
    fetched_at = now(), etag = coalesce($5, etag), last_modified = coalesce($6, last_modified),
    lang = coalesce($7, lang)
WHERE url_hash = $1
"""

SAVE_CONTENT = """
UPDATE pages SET status = $2, title = $3, bm25 = tokenizer_catalog.tokenize($4, 'pt')::bm25vector,
    lang = $5, content_hash = $6, etag = $7, last_modified = $8, check_interval = $9,
    next_check_at = now() + $9, published_at = $10, fetched_at = now(),
    last_changed_at = now(), summary = NULL, fail_count = 0
WHERE url_hash = $1
"""

SAVE_TEXT = """
INSERT INTO texts (page_id, text) VALUES ($1, $2)
ON CONFLICT (page_id) DO UPDATE SET text = EXCLUDED.text
"""

SAVE_LINKS = """
INSERT INTO links (page_id, targets) VALUES ($1, $2)
ON CONFLICT (page_id) DO UPDATE SET targets = EXCLUDED.targets
"""

UPSERT_CHUNK = """
INSERT INTO chunks (page_id, seq, start_ch, end_ch, simhash, priority)
VALUES ($1, $2, $3, $4, $5, embed_priority($6, $7))
ON CONFLICT (page_id, seq) DO UPDATE SET start_ch = EXCLUDED.start_ch, end_ch = EXCLUDED.end_ch,
    simhash = EXCLUDED.simhash, priority = EXCLUDED.priority, embedding = NULL, leased_until = NULL
"""

# Ainda sem vetor na gêmea, a cópia fica pendente e recebe o vetor quando a gêmea for embedada.
COPY_CHUNKS = """
INSERT INTO chunks (page_id, seq, start_ch, end_ch, simhash, embedding, priority)
SELECT $1, seq, start_ch, end_ch, simhash, embedding, embed_priority($3, $4)
FROM chunks WHERE page_id = $2
"""

ADD_FRONTIER = f"""
WITH ins AS (
    INSERT INTO frontier (url_hash, url, host, depth)
    SELECT t.uh, t.u, t.h, t.d
    FROM unnest($1::bigint[], $2::text[], $3::text[], $4::smallint[]) AS t(uh, u, h, d)
    JOIN hosts ho ON ho.host = t.h
    WHERE NOT ho.blocked AND ho.page_count < ho.max_pages AND (ho.priority >= $5 OR ho.seeded)
      AND NOT EXISTS (SELECT 1 FROM pages p WHERE p.url_hash = t.uh)
    ON CONFLICT DO NOTHING
    RETURNING host
), due AS (  -- só os que mudam, travados em ordem: hosts populares eram fonte de deadlock
    SELECT host FROM hosts
    WHERE host IN (SELECT host FROM ins) AND next_due > {COOLDOWN}
    ORDER BY host FOR UPDATE
), moved AS (
    UPDATE hosts h SET next_due = {COOLDOWN} FROM due WHERE h.host = due.host
)
SELECT count(*) FROM ins
"""


@router.post("/batch")
async def batch(hosts: int = 10, per_host: int = 10, name: str = Depends(worker)):
    """Lease hosts with pending work and hand their URLs to the worker.

    per_host is a floor: big hosts get up to sqrt(page_count) URLs, capped at MAX_PER_HOST.
    """
    out = []
    async with db.pool().acquire() as conn, conn.transaction():
        for h in await conn.fetch(LEASE_HOSTS, hosts, LEASE_SECONDS, MIN_PRIORITY):
            n = min(MAX_PER_HOST, max(per_host, int(h["page_count"] ** 0.5)))
            n = max(1, min(n, int(HOST_BUDGET / max(h["crawl_delay"], 1))))
            urls = []
            if h["accepts_new"]:
                urls = await conn.fetch(FRONTIER_URLS, h["host"], n)
            if len(urls) < n:
                urls += await conn.fetch(DUE_URLS, h["host"], n - len(urls))
            if not urls:
                await conn.execute(RELEASE_HOST, h["host"])
                continue
            out.append(
                {
                    "host": h["host"],
                    "crawl_delay": h["crawl_delay"],
                    "robots": h["robots"],
                    "want_sitemaps": h["want_sitemaps"],
                    "urls": [dict(u) for u in urls],
                }
            )
    return {"hosts": out}


@router.post("/results")
async def results(body: FetchResults, name: str = Depends(worker)):
    """Ingest a worker's results: one short transaction per host, then the frontier.

    One transaction for the whole batch held locks for seconds over hosts every page links to;
    with 12 workers it deadlocked five times in a row and the batches snowballed.
    """
    started = time.monotonic()
    outcomes: Counter[str] = Counter()
    queued = 0
    links: dict[int, Links] = {0: {}}  # prioridade herdada pelos hosts novos -> links
    for url in body.urls:
        add_link(links[0], url, 0)
    by_host: dict[str, list[PageResult]] = {}
    for page in body.pages:
        by_host.setdefault(host_of(page.url), []).append(page)
    async with db.pool().acquire() as conn:
        priority = dict(
            await conn.fetch("SELECT host, priority FROM hosts WHERE host = ANY($1)", list(by_host))
        )
        for host, info in sorted(body.hosts.items()):
            await in_tx(conn, save_host, host, info)
        for host in sorted(by_host):
            o, n, found = await in_tx(conn, ingest_pages, by_host[host])
            outcomes += o
            queued += n
            inherited = max(priority.get(host, 0) - PRIORITY_DECAY, 0)
            for h, link in found.items():
                links.setdefault(inherited, {}).setdefault(h, link)
        added = 0
        for inherited, group in sorted(links.items(), reverse=True):
            added += await in_tx(conn, add_frontier, group, inherited)
        for host in sorted(set(by_host) | set(body.hosts)):
            await conn.execute(RELEASE_HOST, host)
        ms = int((time.monotonic() - started) * 1000)
        await metrics.record_batch(conn, name, ms, outcomes, added, queued)
    return {"ok": True}


async def in_tx[T](conn: Conn, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    """Run fn in its own transaction. A deadlock repeats only this piece."""
    for attempt in range(5):
        try:
            async with conn.transaction():
                return await fn(conn, *args)
        except asyncpg.DeadlockDetectedError:
            log.warning("deadlock in %s, retrying (%d)", fn.__name__, attempt + 1)
            await asyncio.sleep(0.2 * (attempt + 1))
    raise HTTPException(503, "Deadlock persistente, reenvie.")


async def save_host(conn: Conn, host: str, info: HostResult) -> None:
    await conn.execute(SAVE_HOST, host, info.robots, info.crawl_delay, info.sitemaps)


async def ingest_pages(conn: Conn, pages: list[PageResult]) -> tuple[Counter[str], int, Links]:
    outcomes: Counter[str] = Counter()
    queued = 0
    links: Links = {}
    for page in pages:
        outcome, n = await ingest(conn, page, links)
        outcomes[outcome] += 1
        queued += n
    return outcomes, queued, links


@router.post("/release")
async def release(body: Release, name: str = Depends(worker)):
    """Hosts that a stopping worker will not finish go back to the queue right away.

    Without this, a restart left them leased for up to 50 minutes and the new worker sat idle.
    """
    async with db.pool().acquire() as conn, conn.transaction():
        for host in sorted(body.hosts):
            await conn.execute(RELEASE_HOST, host)
    return {"released": len(body.hosts)}


@router.post("/seed")
async def seed(body: Seed, name: str = Depends(worker)):
    links: Links = {}
    for url in body.urls:
        add_link(links, url, 0)
    hosts = list({host for _, host, _ in links.values()})
    async with db.pool().acquire() as conn, conn.transaction():
        added = await add_frontier(conn, links, min_priority=0)
        await conn.execute(  # semeado passa na frente da fila de hosts
            "UPDATE hosts SET seeded = true, langs = coalesce($2, langs),"
            + " priority = greatest(priority, $3) WHERE host = ANY($1)",
            sorted(hosts),
            body.langs,
            body.priority,
        )
    return {"added": added}


async def ingest(conn: Conn, page: PageResult, links: Links) -> tuple[str, int]:
    """Store one fetch result. Returns the outcome and how many chunks were queued."""
    h, host = url_hash(page.url), host_of(page.url)
    await conn.execute("DELETE FROM frontier WHERE url_hash = $1", h)
    await conn.execute(INSERT_HOSTS, [host])
    if await conn.fetchval(ENSURE_PAGE, h, page.url, host, page.depth):
        await conn.execute("UPDATE hosts SET page_count = page_count + 1 WHERE host = $1", host)
    row = await conn.fetchrow(
        "SELECT id, content_hash, check_interval, fail_count, rank FROM pages WHERE url_hash = $1",
        h,
    )
    assert row
    interval: dt.timedelta = row["check_interval"]

    if page.final_url and page.final_url != page.url:
        await conn.execute(TOUCH_PAGE, h, 301, 0, DEAD_INTERVAL, None, None, None)
        target = [url_hash(page.final_url)]  # o redirect repassa PageRank para o destino
        await conn.execute(SAVE_LINKS, row["id"], target)
        moved = page.model_copy(update={"url": page.final_url, "final_url": None})
        return await ingest(conn, moved, links)

    # Página sem texto mas com links é um hub, tipo home de portal: o hash é da lista de links.
    content = page.text or "\n".join(page.links)
    digest = content_hash(content) if page.status == 200 and content else None
    if page.status == 304 or (digest and digest == row["content_hash"]):
        interval = min(interval * 2, MAX_INTERVAL)
        await conn.execute(
            TOUCH_PAGE, h, page.status, 0, interval, page.etag, page.last_modified, None
        )
        return "unchanged", 0
    if digest is None:
        fails = MAX_FAILS if page.status == ROBOTS_BLOCKED else row["fail_count"] + 1
        await conn.execute(TOUCH_PAGE, h, page.status, fails, interval, None, None, None)
        return "failed", 0
    langs = await conn.fetchval("SELECT langs FROM hosts WHERE host = $1", host)
    if page.lang not in langs:
        await conn.execute(
            TOUCH_PAGE, h, page.status, 0, DEAD_INTERVAL, page.etag, page.last_modified, page.lang
        )
        await conn.execute(FOREIGN_HOST, host)
        return "foreign", 0

    is_new = row["content_hash"] is None
    if is_new:
        await conn.execute("UPDATE hosts SET pt_count = pt_count + 1 WHERE host = $1", host)
    interval = NEW_INTERVAL if is_new else max(interval / 2, MIN_INTERVAL)
    page_links = page.links[:MAX_LINKS]
    await conn.execute(
        SAVE_CONTENT,
        h,
        page.status,
        page.title,
        page.text,
        page.lang,
        digest,
        page.etag,
        page.last_modified,
        interval,
        published(page.published),
    )
    if page.text is None:  # virou hub
        await conn.execute("DELETE FROM texts WHERE page_id = $1", row["id"])
    else:
        await conn.execute(SAVE_TEXT, row["id"], page.text)
    if page_links:
        await conn.execute(SAVE_LINKS, row["id"], [url_hash(u) for u in page_links])
    else:
        await conn.execute("DELETE FROM links WHERE page_id = $1", row["id"])
    queued = await sync_chunks(conn, row["id"], digest, page.chunks, host, row["rank"])
    for url in page_links:
        add_link(links, url, page.depth + 1)
    return ("new" if is_new else "changed"), queued


async def sync_chunks(
    conn: Conn, page_id: int, digest: bytes | None, chunks: list[Chunk], host: str, rank: float
) -> int:
    """Keep embeddings whose text barely moved; copy them from a page with identical text.

    Returns how many chunks were queued for embedding.
    """
    twin = await conn.fetchval(  # a de menor id é a que a fila do embed atende
        "SELECT id FROM pages WHERE content_hash = $1 AND id <> $2 ORDER BY id LIMIT 1",
        digest,
        page_id,
    )
    if twin:
        await conn.execute("DELETE FROM chunks WHERE page_id = $1", page_id)
        await conn.execute(COPY_CHUNKS, page_id, twin, host, rank)
        return 0
    old = dict(await conn.fetch("SELECT seq, simhash FROM chunks WHERE page_id = $1", page_id))
    queued = 0
    for seq, c in enumerate(chunks):
        if seq in old and hamming(old[seq], c.simhash) <= REEMBED_BITS:
            await conn.execute(
                "UPDATE chunks SET start_ch = $3, end_ch = $4 WHERE page_id = $1 AND seq = $2",
                page_id,
                seq,
                c.start,
                c.end,
            )
        else:
            await conn.execute(UPSERT_CHUNK, page_id, seq, c.start, c.end, c.simhash, host, rank)
            queued += 1
    await conn.execute("DELETE FROM chunks WHERE page_id = $1 AND seq >= $2", page_id, len(chunks))
    return queued


def published(value: str | None) -> dt.date | None:
    """Data extraída da página; futuro ou lixo viram None."""
    try:
        date = dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None
    return date if date and date <= dt.date.today() else None


def add_link(links: Links, url: str, depth: int) -> None:
    if depth <= MAX_DEPTH:
        links.setdefault(url_hash(url), (url, host_of(url), depth))


async def add_frontier(
    conn: Conn, links: Links, priority: int = 0, min_priority: int = MIN_PRIORITY
) -> int:
    """Sorted inserts: two transactions inserting the same rows in different orders deadlock.

    priority > 0 is inherited by hosts never crawled yet, the lite version of focused crawling:
    whoever a nerd site links to is probably nerd too. Links to hosts below min_priority, which
    the lease never serves, stay out: they piled up 5.6 million dead URLs. Seeds pass 0.
    """
    if not links:
        return 0
    rows = sorted(links.items())
    hashes = [h for h, _ in rows]
    urls, hosts, depths = (list(col) for col in zip(*(v for _, v in rows), strict=True))
    if priority > 0:
        await conn.execute(INSERT_HOSTS_PRIORITY, sorted(set(hosts)), priority)
    else:
        await conn.execute(INSERT_HOSTS, sorted(set(hosts)))
    return await conn.fetchval(ADD_FRONTIER, hashes, urls, hosts, depths, min_priority) or 0
