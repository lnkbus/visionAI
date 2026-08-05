"""요약 저장소 구현.

Wave 4는 Redis 기반이다. 요약은 상담 이력 시스템으로 넘어가는 중간 산출물이라
영속 DB(Wave 6 PostgreSQL)가 붙기 전까지는 TTL 있는 저장으로 충분하다.
"""

from __future__ import annotations

from redis.asyncio import Redis

from vai_contracts.summary import Summary

KEY_PREFIX = "vai:summary:"
INDEX_PREFIX = "vai:summary-index:"
TTL_SECONDS = 60 * 60 * 24 * 7
"""7일. 상담 이력 시스템으로 넘어갈 시간은 충분하고, 영속 보관은 그쪽 책임이다."""


class RedisSummaryStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def put(self, summary: Summary) -> None:
        await self._redis.set(
            KEY_PREFIX + summary.session_id, summary.model_dump_json().encode(), ex=TTL_SECONDS
        )
        index = INDEX_PREFIX + summary.tenant_id
        # 최신순 조회를 위해 리스트 앞에 넣고 길이를 제한한다.
        await self._redis.lrem(index, 0, summary.session_id)
        await self._redis.lpush(index, summary.session_id)
        await self._redis.ltrim(index, 0, 999)
        await self._redis.expire(index, TTL_SECONDS)

    async def get(self, session_id: str) -> Summary | None:
        raw = await self._redis.get(KEY_PREFIX + session_id)
        return Summary.model_validate_json(raw) if raw else None

    async def list_for(self, tenant_id: str, limit: int = 50) -> list[Summary]:
        ids = await self._redis.lrange(INDEX_PREFIX + tenant_id, 0, limit - 1)
        summaries: list[Summary] = []
        for raw_id in ids:
            # decode_responses 설정에 따라 bytes로도 str로도 온다.
            session_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            found = await self.get(session_id)
            if found is not None:
                summaries.append(found)
        return summaries


class InMemorySummaryStore:
    """테스트·단일 프로세스용."""

    def __init__(self) -> None:
        self._data: dict[str, Summary] = {}

    async def put(self, summary: Summary) -> None:
        self._data[summary.session_id] = summary

    async def get(self, session_id: str) -> Summary | None:
        return self._data.get(session_id)

    async def list_for(self, tenant_id: str, limit: int = 50) -> list[Summary]:
        matched = [s for s in self._data.values() if s.tenant_id == tenant_id]
        return sorted(matched, key=lambda s: s.created_at, reverse=True)[:limit]
