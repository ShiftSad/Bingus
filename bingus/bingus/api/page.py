import logging

from fastapi import APIRouter, HTTPException

from bingus.api import db, llm
from bingus.api.fetch import add_frontier, add_link
from bingus.common.hashing import url_hash
from bingus.common.urls import normalize

log = logging.getLogger("bingus.page")
router = APIRouter()

PAGE = "SELECT id, url, host, title, text, summary, content_hash FROM pages WHERE url_hash = $1"


@router.get("/page")
async def page(url: str, prompt: str | None = None):
    """A stored page. With a prompt, the LLM answers it over the page text (cached in RAM only)."""
    canonical = normalize(url)
    if not canonical:
        raise HTTPException(400, "URL inválida")
    pool = db.pool()
    row = await pool.fetchrow(PAGE, url_hash(canonical))
    if row is None or row["content_hash"] is None:
        links = {}
        add_link(links, canonical, 0)
        async with pool.acquire() as conn, conn.transaction():
            await add_frontier(conn, links)
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
