"""PageRank sobre pages.out_links. Processo à parte, roda uma vez por dia: bingus-rank."""

import asyncio
import io
import logging
import os
import time

import asyncpg
import numpy as np
import scipy.sparse as sp

log = logging.getLogger("bingus.rank")

DAMPING = 0.85
ITERATIONS = 20
# Só grava rank que mudou mais que isso. Quase todo rank muda um pouco a cada rodada, e cada UPDATE
# em pages é uma linha nova mais uma entrada em cada índice, BM25 incluso: 11 GB de NVMe por rodada.
RANK_TOLERANCE = 0.05


async def stream(conn: asyncpg.Connection, sql: str, cols: int, each) -> None:
    """COPY em texto convertido para numpy por pedaço. Nunca vira lista de Python."""
    tail = b""

    async def sink(chunk: bytes) -> None:
        nonlocal tail
        data = tail + chunk
        cut = data.rfind(b"\n") + 1
        if cut:
            each(np.fromstring(data[:cut].decode(), dtype=np.int64, sep="\t").reshape(-1, cols))
        tail = data[cut:]

    await conn.copy_from_query(sql, output=sink)


async def load(conn: asyncpg.Connection, sql: str, cols: int) -> np.ndarray:
    parts: list[np.ndarray] = []
    await stream(conn, sql, cols, parts.append)
    return np.concatenate(parts) if parts else np.empty((0, cols), np.int64)


def pagerank(n: int, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    out = np.bincount(src, minlength=n)
    # cada página divide 1 entre seus links; por página e depois indexado, sem float64 por link
    weight = (1 / np.maximum(out, 1)).astype(np.float32)[src]
    m = sp.csr_matrix((weight, (dst, src)), shape=(n, n))  # m @ rank = quanto cada uma recebe
    dangling = out == 0  # sem links: distribui para todos, senão o rank vaza
    rank = np.full(n, 1.0 / n, dtype=np.float32)
    for _ in range(ITERATIONS):
        leaked = rank[dangling].sum()
        rank = (1 - DAMPING) / n + DAMPING * (m @ rank + leaked / n)
    return rank * n  # média 1, números legíveis


async def run() -> None:
    started = time.time()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    # mesmo snapshot nas duas leituras: com o crawler gravando, links de páginas novas
    # apontavam para fora da matriz
    # Os links são resolvidos por pedaço do COPY, direto para posições int32: inteiros, os 238
    # milhões de pares de int64 e as cópias temporárias passavam dos 14 GB do container.
    srcs: list[np.ndarray] = []
    dsts: list[np.ndarray] = []
    total = 0

    def resolve(edges: np.ndarray) -> None:
        nonlocal total
        total += len(edges)
        # out_links guarda url_hash do destino; vira posição na matriz por busca binária
        pos = np.searchsorted(sorted_hashes, edges[:, 1])
        pos[pos == n] = 0
        found = sorted_hashes[pos] == edges[:, 1]  # destino que ainda não é página é ignorado
        dsts.append(order[pos[found]].astype(np.int32))
        srcs.append(np.searchsorted(ids, edges[found, 0]).astype(np.int32))

    async with conn.transaction(isolation="repeatable_read", readonly=True):
        nodes = await load(conn, "SELECT id, url_hash FROM pages ORDER BY id", 2)
        ids, hashes = np.ascontiguousarray(nodes[:, 0]), nodes[:, 1]
        n = len(ids)
        if n == 0:
            return
        order = np.argsort(hashes)
        sorted_hashes = hashes[order]
        del nodes, hashes
        await stream(
            conn, "SELECT id, unnest(out_links) FROM pages WHERE out_links IS NOT NULL", 2, resolve
        )
    src, dst = np.concatenate(srcs), np.concatenate(dsts)
    srcs.clear()
    dsts.clear()
    log.info("%d pages, %d links, %d resolved, in %.0fs", n, total, len(src), time.time() - started)

    rank = pagerank(n, src, dst)
    log.info("pagerank done in %.0fs, max %.1f", time.time() - started, rank.max())

    buf = io.BytesIO()
    np.savetxt(buf, np.column_stack([ids, rank]), fmt="%d\t%.6g")
    buf.seek(0)
    await conn.execute("CREATE TEMP TABLE r (id bigint PRIMARY KEY, rank real)")
    await conn.copy_to_table("r", source=buf)
    await conn.execute("ANALYZE r")
    # em lotes com transações curtas: um UPDATE só trava o crawler por meia hora
    for lo in range(0, int(ids.max()) + 1, 50_000):
        await conn.execute(
            "UPDATE pages p SET rank = r.rank FROM r"
            + " WHERE p.id = r.id AND r.id > $1 AND r.id <= $2"
            + " AND abs(p.rank - r.rank) > $3 * p.rank",
            lo,
            lo + 50_000,
            RANK_TOLERANCE,
        )
    for lo in range(0, int(ids.max()) + 1, 50_000):  # fila do embed segue o rank novo
        await conn.execute(
            "UPDATE chunks c SET priority = embed_priority(p.host, p.rank) FROM pages p"
            + " WHERE c.page_id = p.id AND c.embedding IS NULL AND c.page_id > $1"
            + " AND c.page_id <= $2 AND c.priority <> embed_priority(p.host, p.rank)",
            lo,
            lo + 50_000,
        )
    await conn.execute(
        "UPDATE hosts h SET rank = s.rank FROM"
        + " (SELECT host, sum(rank) AS rank FROM pages GROUP BY host) s WHERE h.host = s.host"
    )
    await conn.close()
    log.info("saved in %.0fs", time.time() - started)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())
