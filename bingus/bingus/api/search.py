import asyncio
import datetime as dt
import logging
import math
import os
import re
import time

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request

from bingus.api import db
from bingus.api.page import ensure_summary

log = logging.getLogger("bingus.search")
router = APIRouter()

EMBED_URL = os.environ.get("BINGUS_EMBED_URL", "http://localhost:8100")
CANDIDATES = 200  # por lado antes da fusão; é o máximo que a paginação alcança
RRF_K = 60
FRESH_WEIGHT = 0.5  # bônus máximo, em frações de uma primeira posição da fusão
FRESH_DAYS = 60  # a cada tanto o bônus cai por e
SNIPPET = 300
BURST = 20  # token bucket por IP: aguenta rajada de 20...
REFILL = 1.0  # ...e recarrega um por segundo

QUERY_CACHE_TTL = 24 * 3600
QUERY_CACHE_MAX = 10_000

embedder = httpx.AsyncClient(base_url=EMBED_URL, timeout=10)
buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last seen)
query_vectors: dict[str, tuple[float, str]] = {}  # query normalizada -> (expira em, vetor)

BM25 = """
SELECT id, bm25 <&> to_bm25query('pages_bm25', tokenizer_catalog.tokenize($1, 'pt')) AS score
FROM pages ORDER BY score LIMIT $2
"""

VECTOR = """
SELECT page_id, start_ch, end_ch, embedding <=> quantize_to_rabitq8($1::vector) AS dist
FROM chunks ORDER BY dist LIMIT $2
"""

META = "SELECT id, content_hash, rank, published_at FROM pages WHERE id = ANY($1)"
PAGES = """
SELECT id, url, host, title, text, summary, content_hash, rank, published_at
FROM pages WHERE id = ANY($1)
"""


@router.get("/search")
async def search(
    request: Request, q: str, limit: int = 10, offset: int = 0, summaries: bool = True
):
    """Hybrid search: BM25 on pages and vectors on chunks, fused with reciprocal rank fusion.

    summaries=false skips the LLM: stored summaries still come back, missing ones stay null.
    """
    if not allow(client_ip(request)):
        raise HTTPException(429, "Muitas buscas. Espere um pouco.")
    q = q.strip()
    timer = Timer()
    warnings: list[str] = []
    if not q:
        return {
            "query": q,
            "offset": offset,
            "has_more": False,
            "results": [],
            "timings": {},
            "warnings": warnings,
        }
    pool = db.pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {CANDIDATES}")
            lexical = [r["id"] for r in await conn.fetch(BM25, q, CANDIDATES)]
    except Exception as e:
        log.warning("bm25 failed: %s", e)
        lexical = []
        warnings.append("Busca por palavras falhou no banco.")
    timer.lap("bm25")
    best_chunk: dict[int, tuple[int, int]] = {}
    vec = await embed_query(q)
    timer.lap("embed_query")
    if not vec:
        warnings.append("Embed worker fora do ar: busca só por palavras, sem semântica.")
    if len(warnings) == 2:
        raise HTTPException(503, "Busca indisponível: banco e embed worker fora do ar.")
    if vec:
        for r in await pool.fetch(VECTOR, vec, CANDIDATES):
            best_chunk.setdefault(r["page_id"], (r["start_ch"], r["end_ch"]))
    timer.lap("vector")
    semantic = list(best_chunk)
    candidates = list(dict.fromkeys(lexical + semantic))
    if not candidates:
        return {
            "query": q,
            "offset": offset,
            "has_more": False,
            "results": [],
            "timings": timer.done(),
            "warnings": warnings,
        }
    meta = {r["id"]: r for r in await pool.fetch(META, candidates)}
    by_rank = sorted(candidates, key=lambda i: -meta[i]["rank"])  # PageRank como terceira lista

    scores: dict[int, float] = {}
    for ranking in (lexical, semantic, by_rank):
        for position, page_id in enumerate(ranking):
            scores[page_id] = scores.get(page_id, 0) + 1 / (RRF_K + position)
    today = dt.date.today()
    for page_id in candidates:  # bônus por recência: vale meia primeira posição e decai em meses
        if meta[page_id]["published_at"]:
            age = (today - meta[page_id]["published_at"]).days
            scores[page_id] += FRESH_WEIGHT / RRF_K * math.exp(-max(age, 0) / FRESH_DAYS)

    ordered = []
    seen: set[bytes] = set()  # mesma página em URLs diferentes aparece uma vez
    for page_id in sorted(scores, key=lambda i: -scores[i]):
        if meta[page_id]["content_hash"] not in seen:
            seen.add(meta[page_id]["content_hash"])
            ordered.append(page_id)
    chosen = ordered[offset : offset + limit]
    timer.lap("fusion")
    pages = {r["id"]: r for r in await pool.fetch(PAGES, chosen)}
    timer.lap("pages")

    if summaries:
        texts = await asyncio.gather(*(summary_for(pages[i]) for i in chosen))
    else:
        texts = [pages[i]["summary"] for i in chosen]
    timer.lap("summaries")
    results = []
    for page_id, summary in zip(chosen, texts, strict=True):
        p = pages[page_id]
        results.append(
            {
                "url": p["url"],
                "title": p["title"],
                "host": p["host"],
                "snippet": snippet(p["text"], q, best_chunk.get(page_id)),
                "summary": summary,
                "score": round(scores[page_id], 4),
                "rank": round(p["rank"], 3),
                "published": p["published_at"].isoformat() if p["published_at"] else None,
                "bm25_rank": lexical.index(page_id) + 1 if page_id in lexical else None,
                "vector_rank": semantic.index(page_id) + 1 if page_id in semantic else None,
            }
        )
    return {
        "query": q,
        "offset": offset,
        "has_more": offset + limit < len(ordered),
        "results": results,
        "timings": timer.done(),
        "warnings": warnings,
    }


class Timer:
    """Milissegundos por etapa, para a resposta mostrar onde o tempo foi."""

    def __init__(self) -> None:
        self.start = self.last = time.perf_counter()
        self.laps: dict[str, int] = {}

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.laps[name] = round((now - self.last) * 1000)
        self.last = now

    def done(self) -> dict[str, int]:
        self.laps["total"] = round((time.perf_counter() - self.start) * 1000)
        return self.laps


async def summary_for(p: asyncpg.Record) -> str | None:
    """Stored summary, or one generated now. Results are summarized in parallel, on first access."""
    if p["summary"] or not p["text"]:
        return p["summary"]
    try:
        return await asyncio.wait_for(ensure_summary(p["id"], p["text"], p["content_hash"]), 20)
    except TimeoutError:
        return None


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")  # Caddy na frente
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def allow(ip: str) -> bool:
    now = time.monotonic()
    tokens, last = buckets.get(ip, (BURST, now))
    tokens = min(BURST, tokens + (now - last) * REFILL)
    if len(buckets) > 10_000:  # limpa quem já recarregou por completo
        buckets.clear()
    if tokens < 1:
        buckets[ip] = (tokens, now)
        return False
    buckets[ip] = (tokens - 1, now)
    return True


async def embed_query(q: str) -> str | None:
    """Query vector as a pgvector literal, or None when the embed worker is unreachable.

    Cached in RAM for a day: the same query again never touches the GPU.
    """
    key = " ".join(q.lower().split())
    hit = query_vectors.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        r = await embedder.post("/embed", json={"texts": [q]})
        r.raise_for_status()
        vec = "[" + ",".join(map(str, r.json()["embeddings"][0])) + "]"
    except Exception as e:
        log.warning("embed worker unavailable, lexical only: %s", e)
        return None
    if len(query_vectors) >= QUERY_CACHE_MAX:
        query_vectors.clear()
    query_vectors[key] = (time.monotonic() + QUERY_CACHE_TTL, vec)
    return vec


def snippet(text: str | None, q: str, chunk: tuple[int, int] | None) -> str | None:
    """A window of the best chunk around the first query word, or the start of the page."""
    if not text:
        return None
    body = text[chunk[0] : chunk[1]] if chunk else text.split("\n", 1)[-1]
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    m = re.search("|".join(map(re.escape, words)), body, re.I) if words else None
    start = max(0, m.start() - SNIPPET // 3) if m else 0
    return body[start : start + SNIPPET].strip()
