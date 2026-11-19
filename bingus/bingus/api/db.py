import asyncio
import logging
import os

import asyncpg

log = logging.getLogger("bingus.db")
_pool: asyncpg.Pool | None = None


async def connect() -> None:
    """Espera o Postgres. Depois de uma queda ele leva minutos recuperando o WAL; caindo aqui, a API
    dependia do backoff de restart do Docker, que chegava a um minuto entre tentativas."""
    global _pool
    for attempt in range(600):
        try:
            _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2, max_size=10)
            return
        except (OSError, asyncpg.CannotConnectNowError) as e:
            if attempt == 599:
                raise
            if attempt % 30 == 0:
                log.warning("postgres ainda não aceita conexões: %s", e)
            await asyncio.sleep(2)


async def close() -> None:
    await pool().close()


def pool() -> asyncpg.Pool:
    assert _pool is not None, "db.connect() not called"
    return _pool
