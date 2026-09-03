"""Seed da Wikipédia pelo dump HTML do Wikimedia Enterprise.

    bingus-seed wikipedia data/ptwiki.tar.gz [pular N]

Cada artigo passa pelo mesmo extrator e pela mesma ingestão de uma página buscada pelo crawler,
sem nenhum request na Wikipédia. Links internos não vão para a frontier, o dump já traz os
artigos; links externos só entram se forem .br.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import sys
import tarfile
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import asyncpg
from fastapi import APIRouter

from bingus.api import db
from bingus.api.fetch import RELEASE_HOST, Links, PageResult, add_frontier, ingest
from bingus.common.urls import normalize
from bingus.fetch.page import extract

log = logging.getLogger("bingus.seed")

HOST = "pt.wikipedia.org"
SUFFIX = " – Wikipédia, a enciclopédia livre"
BATCH = 64
WORKERS = int(os.environ.get("BINGUS_SEED_WORKERS", "4"))  # processos de extração
REVISIT = dt.timedelta(days=90)  # revisitas espalhadas por esse período

# Progresso para o painel em /seed/status. Criada aqui para não exigir recriar o banco.
PROGRESS_TABLE = """
CREATE TABLE IF NOT EXISTS seed_progress (
    name        text PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    done        integer NOT NULL,
    skipped     integer NOT NULL,
    rate        real NOT NULL,
    bytes_read  bigint NOT NULL,
    bytes_total bigint NOT NULL,
    finished    boolean NOT NULL
)
"""
SAVE_PROGRESS = """
INSERT INTO seed_progress VALUES ('wikipedia', $1, now(), $2, $3, $4, $5, $6, $7)
ON CONFLICT (name) DO UPDATE SET started_at = $1, updated_at = now(), done = $2, skipped = $3,
    rate = $4, bytes_read = $5, bytes_total = $6, finished = $7
"""
STATUS = """
SELECT p.*, h.page_count, h.pt_count,
    (SELECT count(*) FROM chunks WHERE embedding IS NULL) AS chunks_pending
FROM seed_progress p LEFT JOIN hosts h ON h.host = 'pt.wikipedia.org' WHERE p.name = 'wikipedia'
"""
RECENT = """
SELECT url, title, length(text) AS chars, fetched_at FROM pages
WHERE host = 'pt.wikipedia.org' ORDER BY id DESC LIMIT 20
"""

router = APIRouter()


@router.get("/seed/status")
async def status():
    """Progresso da carga da Wikipédia e os últimos artigos que entraram."""
    pool = db.pool()
    row = await pool.fetchrow(STATUS)
    if row is None:
        return {"progress": None, "recent": []}
    progress = dict(row)
    left = progress["bytes_total"] - progress["bytes_read"]
    speed = progress["bytes_read"] / max(
        (progress["updated_at"] - progress["started_at"]).total_seconds(), 1
    )
    progress["eta_s"] = None if progress["finished"] or speed == 0 else int(left / speed)
    recent = [dict(r) for r in await pool.fetch(RECENT)]
    return {"progress": progress, "recent": recent}


def parse(line: bytes) -> dict[str, Any] | None:
    """Roda nos processos filhos: JSON -> página extraída, ou None para redirects e vazios."""
    doc = json.loads(line)
    html = doc.get("article_body", {}).get("html", "")
    url = normalize(doc.get("url", ""))
    if not url or not html or 'rel="mw:PageProp/redirect"' in html:
        return None
    page = extract(html.encode(), url)
    if not page or not page["text"]:
        return None
    page.pop("final_url", None)
    page["lang"] = "pt"  # o detector erra em artigos curtos cheios de nomes e números
    if page["title"] and page["title"].endswith(SUFFIX):
        page["title"] = page["title"].removesuffix(SUFFIX)
        page["text"] = page["text"].replace(SUFFIX, "", 1)
    return {"url": url, "status": 200, **page}


class Dump:
    """Artigos do tar.gz em streaming, contando bytes lidos. Arquivo truncado só encerra antes."""

    def __init__(self, path: str, skip: int) -> None:
        self.file = open(path, "rb")
        self.total = os.path.getsize(path)
        self.skip = skip

    @property
    def read(self) -> int:
        return self.file.tell()

    def __iter__(self) -> Iterator[bytes]:
        n = 0
        try:
            with tarfile.open(fileobj=self.file, mode="r|gz") as tar:
                for member in tar:
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    for line in f:
                        n += 1
                        if n > self.skip:
                            yield line
        except (tarfile.ReadError, EOFError) as e:
            log.warning("dump ended early: %s", e)


def batches(source: Iterator[bytes]) -> Iterator[list[bytes]]:
    batch: list[bytes] = []
    for line in source:
        batch.append(line)
        if len(batch) == BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


async def save(
    conn: asyncpg.pool.PoolConnectionProxy,
    pages: list[dict[str, Any] | None],
    done: int,
    skipped: int,
) -> tuple[int, int]:
    for attempt in range(5):
        links: Links = {}
        d, s = done, skipped
        try:
            async with conn.transaction():
                for p in pages:
                    if p is None:
                        s += 1
                        continue
                    await ingest(conn, PageResult(**p), links)
                    d += 1
                external = {h: v for h, v in links.items() if v[1].endswith(".br")}
                await add_frontier(conn, external)
            return d, s
        except asyncpg.DeadlockDetectedError:  # disputa com o fetch worker: repete o lote
            log.warning("deadlock, retrying batch (%d)", attempt + 1)
            await asyncio.sleep(2)
    raise RuntimeError("batch kept deadlocking")


async def run(path: str, skip: int) -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    assert pool
    await pool.execute(
        "INSERT INTO hosts (host, max_pages, robots_at) VALUES ($1, 2000000, now())"
        + " ON CONFLICT (host) DO UPDATE SET max_pages = 2000000",
        HOST,
    )
    await pool.execute(PROGRESS_TABLE)
    loop = asyncio.get_running_loop()
    workers = ProcessPoolExecutor(WORKERS)
    done, skipped = skip, 0  # ao retomar, o que foi pulado conta como feito no painel
    started = time.time()
    started_at = dt.datetime.now(dt.UTC)
    dump = Dump(path, skip)
    async with pool.acquire() as conn:
        # o lote seguinte é extraído nos processos enquanto o atual é gravado no banco
        pending: list[asyncio.Future[dict[str, Any] | None]] = []
        for batch in batches(iter(dump)):
            parsing = [loop.run_in_executor(workers, parse, b) for b in batch]
            if pending:
                done, skipped = await save(conn, await asyncio.gather(*pending), done, skipped)
                rate = (done + skipped) / (time.time() - started)
                await conn.execute(
                    SAVE_PROGRESS, started_at, done, skipped, rate, dump.read, dump.total, False
                )
                if done % 1000 < BATCH:
                    log.info("%d pages, %d skipped, %.0f/s", done, skipped, rate)
            pending = parsing
        if pending:
            done, skipped = await save(conn, await asyncio.gather(*pending), done, skipped)
        rate = (done + skipped) / (time.time() - started)
        await conn.execute(
            SAVE_PROGRESS, started_at, done, skipped, rate, dump.total, dump.total, True
        )
        # em lotes: um UPDATE só sobre um milhão de linhas dá deadlock com o fetch worker
        max_id = await conn.fetchval("SELECT max(id) FROM pages") or 0
        for lo in range(0, max_id + 1, 50_000):
            await conn.execute(
                "UPDATE pages SET check_interval = $2::interval,"
                + " next_check_at = now() + random() * $2::interval"
                + " WHERE host = $1 AND id > $3 AND id <= $4 AND check_interval <> $2::interval",
                HOST,
                REVISIT,
                lo,
                lo + 50_000,
            )
        await conn.execute(RELEASE_HOST, HOST)
    workers.shutdown()
    log.info("done: %d pages, %d skipped in %.0fs", done, skipped, time.time() - started)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
    if len(sys.argv) < 3 or sys.argv[1] != "wikipedia":
        sys.exit(__doc__)
    asyncio.run(run(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0))


if __name__ == "__main__":
    main()
