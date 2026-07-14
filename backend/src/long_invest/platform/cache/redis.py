from functools import lru_cache

from redis.asyncio import Redis

from long_invest.platform.config.settings import get_settings


class RedisProbe:
    def __init__(self, url: str) -> None:
        self._client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()


@lru_cache
def get_redis_probe() -> RedisProbe:
    return RedisProbe(get_settings().redis_url)

