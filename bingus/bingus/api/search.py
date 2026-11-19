import asyncio
import datetime as dt
import logging
import math
import os
import re
import time
import unicodedata
from collections import Counter

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from bingus.api import db
from bingus.api.deps import search_key
from bingus.api.page import ensure_summary
from bingus.common.hashing import url_hash

log = logging.getLogger("bingus.search")
router = APIRouter()

EMBED_URL = os.environ.get("BINGUS_EMBED_URL", "http://localhost:8100")
CANDIDATES = 200  # por lado antes da fusão; é o máximo que a paginação alcança
VECTOR_CHUNKS = 400  # chunks de 1700 caracteres: umas 3 por página, então mais que CANDIDATES
RRF_K = 60
FRESH_WEIGHT = 0.5  # bônus máximo, em frações de uma primeira posição da fusão
FRESH_DAYS = 60  # a cada tanto o bônus cai por e
RANK_WEIGHT = 0.5  # PageRank pesa metade de BM25 e vetorial: autoridade não é relevância
MAX_PER_HOST = 2  # um site ocupa no máximo isso antes de os outros aparecerem
# Palavras da busca no título ou na URL, sem acento e com números inteiros: o tokenizador do BM25
# quebra "5060" em dígitos e "custo-benefício" em tokens diferentes de "custo benefício". Vale até
# uma primeira posição. Medido em 40 buscas nerd: nDCG@10 0,42 -> 0,55 sozinho.
TITLE_WEIGHT = 1.0
TITLE_STATS_TTL = 3600
TITLE_SAMPLE = "SELECT title FROM pages TABLESAMPLE SYSTEM (5) WHERE title IS NOT NULL LIMIT 100000"
TEXT_SAMPLE = """
SELECT left(text, 3000) AS text FROM pages TABLESAMPLE SYSTEM (1) WHERE text IS NOT NULL LIMIT 20000
"""
# O tokenizador do BM25 não tem nada em comum entre "memoria" e "memória", e quem busca costuma
# digitar sem acento. A busca troca a palavra pela grafia acentuada quando o corpus quase sempre a
# escreve assim. Medido em títulos e textos: "historia" 9 x 2276, "memoria" 7 x 993; "video" fica
# em 3,3 por causa do inglês. Quem tem as duas leituras comuns nunca troca: "pais" (5,3) é pai e
# país, "esta" é este e está, "pe" é Pernambuco.
ACCENT_RATIO = 2.5
NO_ACCENT = {"pais", "esta", "pe"}
ENGLISH = set("the of to in for and on how what with is are from".split())  # "text to video model"
STOPWORDS = set(
    "a o as os um uma de da do das dos em no na nos nas para pra por com sem e ou que como qual"
    " quais é ser se ao aos à às the of to in for and on how what".split()
)
# Recência só quando a busca pede: com o índice todo novo, o bônus em toda busca era ruído.
FRESH_WORDS = set(
    "hoje agora noticia noticias novo nova novos novas lancamento lancamentos ultimo ultima"
    " ultimos ultimas recente recentes semana anuncia anunciado anunciada news latest".split()
)
# Tag, perfil, autor, categoria e busca interna: quase sempre lixo no topo. Paginação não entra:
# /page/N também é tópico de fórum, que costuma ser bom.
LOW_VALUE = re.compile(
    r"/(tags?|categoria|categorias|category|categories|profile|perfil|author|autor|autores"
    r"|discover|busca|search|arquivo|archive)(/|$)|[?&](s|q|search|busca)=",
    re.I,
)
LOW_VALUE_FACTOR = 0.5
SNIPPET = 300
BURST = 20  # token bucket por IP: aguenta rajada de 20...
REFILL = 1.0  # ...e recarrega um por segundo

QUERY_CACHE_TTL = 24 * 3600
QUERY_CACHE_MAX = 10_000

embedder = httpx.AsyncClient(base_url=EMBED_URL, timeout=10)
buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last seen)
query_vectors: dict[str, tuple[float, str]] = {}  # query normalizada -> (expira em, vetor)


class TitleStats:
    """Em quantos títulos de uma amostra do índice cada palavra aparece, e a grafia acentuada usual
    de cada palavra. Renovada de hora em hora, em segundo plano; vazia, todos os termos pesam igual
    e nada é corrigido."""

    def __init__(self) -> None:
        self.df: Counter[str] = Counter()
        self.docs = 0
        self.accents: dict[str, str] = {}  # "memoria" -> "memória"
        self.at = -TITLE_STATS_TTL
        self.task: asyncio.Task[None] | None = None


titles = TitleStats()

BM25 = """
SELECT id, bm25 <&> to_bm25query('pages_bm25', tokenizer_catalog.tokenize($1, 'pt')) AS score
FROM pages ORDER BY score LIMIT $2
"""

VECTOR = """
SELECT page_id, start_ch, end_ch, embedding <=> quantize_to_rabitq8($1::vector) AS dist
FROM chunks ORDER BY dist LIMIT $2
"""

META = """
SELECT id, url, title, host, content_hash, rank, published_at FROM pages WHERE id = ANY($1)
"""
# Busca curta que é o nome de um site ("tabnews", "radargpu", "clube do hardware fórum"): a raiz
# dele vai para o topo, mesmo sendo um hub sem texto, que nem o BM25 nem o vetorial enxergam.
NAVIGATIONAL = """
WITH h AS (
    SELECT host, split_part(regexp_replace(host, '^www\\.', ''), '.', 1) AS label, page_count
    FROM hosts WHERE NOT blocked AND pt_count > 0
)
SELECT host FROM h
WHERE $1 LIKE label || '%' AND length(label) >= greatest(4, 0.6 * length($1))
ORDER BY page_count DESC LIMIT 1
"""
ROOT_PAGE = "SELECT id FROM pages WHERE url_hash = ANY($1) AND content_hash IS NOT NULL LIMIT 1"
EMBEDDED = "SELECT DISTINCT page_id FROM chunks WHERE page_id = ANY($1) AND embedding IS NOT NULL"
PAGES = """
SELECT id, url, host, title, text, summary, content_hash, rank, published_at
FROM pages WHERE id = ANY($1)
"""


@router.get("/search", dependencies=[Depends(search_key)])
async def search(
    request: Request,
    q: str,
    limit: int = 10,
    offset: int = 0,
    summaries: bool = True,
    format: str | None = None,
    pageno: int = 1,
    boosts: bool = True,
):
    """Hybrid search: BM25 on pages and vectors on chunks, fused with reciprocal rank fusion.

    summaries=false skips the LLM: stored summaries still come back, missing ones stay null.
    format=json answers like SearXNG (pageno pages), which Open WebUI, LangChain and friends
    already speak: pointing them at Bingus as if it were a SearXNG is enough.
    boosts=false turns off title/URL, navigational and low-value adjustments and applies recency
    to every query, the fusion before them: for A/B evaluation.
    """
    if not allow(client_ip(request)):
        raise HTTPException(429, "Muitas buscas. Espere um pouco.")
    if format == "json":
        res = await run_search(q, limit, offset or (max(pageno, 1) - 1) * limit, False, boosts)
        return searxng(res)
    return await run_search(q, limit, offset, summaries, boosts)


def searxng(res: dict) -> dict:
    results = [
        {
            "url": r["url"],
            "title": r["title"] or r["url"],
            "content": r["summary"] or r["snippet"] or "",
            "engine": "bingus",
            "engines": ["bingus"],
            "category": "general",
            "score": r["score"],
            "publishedDate": r["published"],
        }
        for r in res["results"]
    ]
    return {
        "query": res["query"],
        "number_of_results": len(results),
        "results": results,
        "answers": [],
        "corrections": [res["corrected"]] if res.get("corrected") else [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": [["bingus-vetorial", w] for w in res["warnings"]],
    }


async def run_search(
    q: str, limit: int = 10, offset: int = 0, summaries: bool = True, boosts: bool = True
) -> dict:
    """The search itself, without the per-IP rate limit. The MCP calls this directly."""
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
    corrected = correct(q) if boosts else None
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL bm25_catalog.bm25_limit = {CANDIDATES}")
            lexical = [r["id"] for r in await conn.fetch(BM25, corrected or q, CANDIDATES)]
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
        for r in await pool.fetch(VECTOR, vec, VECTOR_CHUNKS):
            best_chunk.setdefault(r["page_id"], (r["start_ch"], r["end_ch"]))
    timer.lap("vector")
    semantic = list(best_chunk)
    nav = await navigational(pool, q) if boosts else None
    candidates = list(dict.fromkeys(([nav] if nav else []) + lexical + semantic))
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
    for ranking, weight in ((lexical, 1.0), (semantic, 1.0), (by_rank, RANK_WEIGHT)):
        for position, page_id in enumerate(ranking):
            scores[page_id] = scores.get(page_id, 0) + weight / (RRF_K + position)
    if vec:
        # Página ainda na fila do embed não tem como aparecer na lista vetorial: a posição lexical
        # vale pelas duas. Sem isso, com o embed atrás do crawler, o primeiro do BM25 perdia para
        # qualquer home antiga já embedada que aparecesse lá pelo meio das duas listas.
        embedded = {r["page_id"] for r in await pool.fetch(EMBEDDED, lexical)}
        for position, page_id in enumerate(lexical):
            if page_id not in embedded:
                scores[page_id] += 1 / (RRF_K + position)
    terms = [t for t in words(q) if t not in STOPWORDS] if boosts else []
    fresh = not boosts or any(t in FRESH_WORDS or re.fullmatch(r"20\d\d", t) for t in terms)
    today = dt.date.today()
    found = {i: set(words(f"{meta[i]['title'] or ''} {meta[i]['url']}")) for i in candidates}
    # Termo raro no corpus pesa mais: "rtx" e "gta" mais que "vale", "comprar" ou "requisitos".
    # Raridade entre os candidatos invertia isso: o termo que os trouxe aparece em quase todos.
    if time.monotonic() - titles.at > TITLE_STATS_TTL:
        titles.at = time.monotonic()
        titles.task = asyncio.create_task(refresh_titles(pool))  # referência: não some no GC
    weight = {t: math.log(1 + (titles.docs + 1) / (1 + titles.df[t])) for t in terms}
    total = sum(weight.values()) or 1
    for page_id in candidates:
        m = meta[page_id]
        scores.setdefault(page_id, 0.0)
        if terms:
            matched = sum(w for t, w in weight.items() if t in found[page_id])
            scores[page_id] += TITLE_WEIGHT / RRF_K * matched / total
        if fresh and m["published_at"]:  # vale meia primeira posição e decai em meses
            age = (today - m["published_at"]).days
            scores[page_id] += FRESH_WEIGHT / RRF_K * math.exp(-max(age, 0) / FRESH_DAYS)
        if boosts and LOW_VALUE.search(m["url"]):
            scores[page_id] *= LOW_VALUE_FACTOR
    if nav:
        scores[nav] = max(scores.values()) + 1 / RRF_K

    ordered, overflow = [], []
    seen: set[bytes] = set()  # mesma página em URLs diferentes aparece uma vez
    per_host: Counter[str] = Counter()  # o excesso de um site vai para o fim, não some
    for page_id in sorted(scores, key=lambda i: -scores[i]):
        m = meta[page_id]
        if m["content_hash"] in seen:
            continue
        seen.add(m["content_hash"])
        per_host[m["host"]] += page_id != nav  # a raiz navegacional não gasta a cota do site
        (ordered if per_host[m["host"]] <= MAX_PER_HOST else overflow).append(page_id)
    ordered += overflow
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
                "snippet": snippet(p["text"], corrected or q, best_chunk.get(page_id)),
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
        "corrected": corrected,
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


async def refresh_titles(pool: asyncpg.Pool) -> None:
    try:
        rows = await pool.fetch(TITLE_SAMPLE)
        texts = [r["text"] for r in await pool.fetch(TEXT_SAMPLE)]
    except Exception as e:
        log.warning("title stats failed: %s", e)
        return
    heads = [r["title"] for r in rows]
    # uns segundos de CPU: numa thread, para não parar as buscas em andamento
    df, accents = await asyncio.to_thread(count_words, heads, texts)
    titles.df, titles.docs, titles.accents = df, len(rows), accents
    log.info("title stats: %d titles, %d words, %d accents", len(rows), len(df), len(accents))


def count_words(heads: list[str], texts: list[str]) -> tuple[Counter[str], dict[str, str]]:
    df: Counter[str] = Counter()
    for title in heads:
        df.update(set(words(title)))
    spelled: Counter[str] = Counter()
    for text in heads + texts:
        spelled.update(re.findall(r"[^\W\d_]+", text.lower()))
    accents: dict[str, str] = {}
    for word, n in spelled.most_common():  # a grafia mais comum de cada palavra vem primeiro
        bare = plain(word)
        usual = n >= 3 and n >= ACCENT_RATIO * spelled[bare]
        if bare and bare != word and bare not in accents and usual:
            accents[bare] = word
    return df, accents


def correct(q: str) -> str | None:
    """The query with the corpus' usual accents, or None when nothing changes."""
    if ENGLISH & set(re.findall(r"[a-z]+", q.lower())):
        return None

    def fix(m: re.Match[str]) -> str:
        word = m.group().lower()
        keep = word in STOPWORDS or word in NO_ACCENT
        return m.group() if keep else titles.accents.get(word, m.group())

    fixed = re.sub(r"[^\W\d_]+", fix, q)
    return fixed if fixed != q else None


def plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def words(text: str) -> list[str]:
    """Lowercase terms without accents; numbers stay whole and hyphens split words."""
    return re.findall(r"[a-z0-9]+", plain(text.lower()))


async def navigational(pool: asyncpg.Pool, q: str) -> int | None:
    """Id of a site's root page when a short query is that site's name."""
    parts = words(q)
    if not 1 <= len(parts) <= 4:
        return None
    host = await pool.fetchval(NAVIGATIONAL, "".join(parts))
    if not host:
        return None
    roots = [url_hash(f"{scheme}://{host}/") for scheme in ("https", "http")]
    return await pool.fetchval(ROOT_PAGE, roots)


def snippet(text: str | None, q: str, chunk: tuple[int, int] | None) -> str | None:
    """A window of the best chunk around the first query word, or the start of the page."""
    if not text:
        return None
    body = text[chunk[0] : chunk[1]] if chunk else text.split("\n", 1)[-1]
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    m = re.search("|".join(map(re.escape, words)), body, re.I) if words else None
    start = max(0, m.start() - SNIPPET // 3) if m else 0
    return body[start : start + SNIPPET].strip()
