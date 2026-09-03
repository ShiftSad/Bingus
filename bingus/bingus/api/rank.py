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


async def load(conn: asyncpg.Connection, sql: str, cols: int) -> np.ndarray:
    """COPY em texto convertido para numpy por pedaço. Nunca vira lista de Python."""
    parts: list[np.ndarray] = []
    tail = b""

    async def sink(chunk: bytes) -> None:
        nonlocal tail
        data = tail + chunk
        cut = data.rfind(b"\n") + 1
        parts.append(np.fromstring(data[:cut].decode(), dtype=np.int64, sep="\t"))
        tail = data[cut:]

    await conn.copy_from_query(sql, output=sink)
    return np.concatenate(parts).reshape(-1, cols) if parts else np.empty((0, cols), np.int64)


def pagerank(n: int, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    out = np.bincount(src, minlength=n)
    weight = (1.0 / out[src]).astype(np.float32)  # cada página divide 1 entre seus links
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
    nodes = await load(conn, "SELECT id, url_hash FROM pages ORDER BY id", 2)
    edges = await load(
        conn, "SELECT id, unnest(out_links) FROM pages WHERE out_links IS NOT NULL", 2
    )
    ids, hashes = nodes[:, 0], nodes[:, 1]
    n = len(ids)
    log.info("%d pages, %d links, loaded in %.0fs", n, len(edges), time.time() - started)
    if n == 0:
        return

    # out_links guarda url_hash do destino; resolve para posição na matriz por busca binária
    order = np.argsort(hashes)
    sorted_hashes = hashes[order]
    pos = np.searchsorted(sorted_hashes, edges[:, 1])
    pos[pos == n] = 0
    found = sorted_hashes[pos] == edges[:, 1]  # destino que ainda não é página é ignorado
    dst = order[pos[found]].astype(np.int32)
    src = np.searchsorted(ids, edges[found, 0]).astype(np.int32)
    del edges, pos, found
    log.info("%d links resolved", len(src))

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
            + " WHERE p.id = r.id AND r.id > $1 AND r.id <= $2 AND p.rank <> r.rank",
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
