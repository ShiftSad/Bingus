import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from bingus.api import db, llm
from bingus.api.deps import search_key
from bingus.api.fetch import add_frontier, add_link
from bingus.common.hashing import url_hash
from bingus.common.urls import normalize
from bingus.fetch.main import MAX_BYTES, UA
from bingus.fetch.page import extract

log = logging.getLogger("bingus.page")
router = APIRouter()

PAGE = """
SELECT p.id, p.url, p.host, p.title, t.text, p.summary, p.content_hash
FROM pages p LEFT JOIN texts t ON t.page_id = p.id WHERE p.url_hash = $1
"""
EXTRACT = ThreadPoolExecutor(max_workers=1)  # lxml em várias threads ao mesmo tempo dá segfault
http = httpx.AsyncClient(
    headers={"User-Agent": UA}, timeout=15, follow_redirects=True, max_redirects=5
)


@router.get("/page", dependencies=[Depends(search_key)])
async def page(url: str, prompt: str | None = None):
    """A stored page. With a prompt, the LLM answers it over the page text (cached in RAM only)."""
    canonical = normalize(url)
    if not canonical:
        raise HTTPException(400, "URL inválida")
    pool = db.pool()
    row = await pool.fetchrow(PAGE, url_hash(canonical))
    if row is None or row["content_hash"] is None:
        await enqueue(canonical)
        raise HTTPException(404, "Página desconhecida. Entrou na fila para ser buscada.")

    summary = row["summary"]
    if summary is None and row["text"]:
        summary = await ensure_summary(row["id"], row["text"], row["content_hash"])
    answer = None
    if prompt and row["text"]:
        try:
            answer = await llm.ask(row["content_hash"], row["text"], prompt)
        except Exception as e:
            log.warning("answer failed: %s", e)
    return {
        "url": row["url"],
        "title": row["title"],
        "host": row["host"],
        "summary": summary,
        "answer": answer,
        "text": None if prompt else row["text"],
    }


async def enqueue(url: str) -> None:
    """Put an unknown page in the frontier, so the crawler fetches and indexes it."""
    links = {}
    add_link(links, url, 0)
    async with db.pool().acquire() as conn, conn.transaction():
        await add_frontier(conn, links)


async def fetch_live(url: str) -> dict[str, Any] | None:
    """Fetch and extract a page right now, for someone asking about a URL outside the index.

    One request on behalf of a person, like a browser would; the crawler still respects robots.
    """
    try:
        async with http.stream("GET", url) as r:
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                return None
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return None
            final = normalize(str(r.url)) or url
    except httpx.HTTPError as e:
        log.info("live fetch of %s failed: %s", url, e)
        return None
    page = await asyncio.get_running_loop().run_in_executor(EXTRACT, extract, bytes(body), final)
    return {**page, "url": final} if page else None


async def ensure_summary(page_id: int, text: str, content_hash: bytes) -> str | None:
    """Generate and persist the summary. The content_hash guard skips pages that just changed."""
    try:
        summary = await llm.summarize(text)
    except Exception as e:
        log.warning("summary failed: %s", e)
        return None
    if summary:
        await db.pool().execute(
            "UPDATE pages SET summary = $2 WHERE id = $1 AND content_hash = $3",
            page_id,
            summary,
            content_hash,
        )
    return summary
