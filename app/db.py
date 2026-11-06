from pathlib import Path

from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


async def init_db(dsn: str) -> None:
    global _pool
    _pool = AsyncConnectionPool(dsn, min_size=1, max_size=10, open=False)
    await _pool.open(wait=True)
    async with _pool.connection() as conn:
        await conn.execute(SCHEMA_PATH.read_text())


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("init_db() was not called")
    return _pool
