import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def connect() -> None:
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=2, max_size=10)


async def close() -> None:
    await pool().close()


def pool() -> asyncpg.Pool:
    assert _pool is not None, "db.connect() not called"
    return _pool
