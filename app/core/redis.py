from __future__ import annotations

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import settings


def get_redis_settings() -> RedisSettings:
    url = settings.REDIS_URL
    # Parse redis://host:port/db
    url = url.removeprefix("redis://")
    host_port, _, db = url.partition("/")
    host, _, port = host_port.partition(":")
    return RedisSettings(
        host=host or "redis",
        port=int(port) if port else 6379,
        database=int(db) if db else 0,
    )


_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(get_redis_settings())
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
