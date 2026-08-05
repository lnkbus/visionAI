"""팝업 피드백 저장소.

상담원이 팝업을 실제로 썼는지가 품질 개선의 **유일한 객관 신호**다. 이게
없으면 무엇을 고쳐야 하는지 아는 방법이 상담원 인터뷰밖에 없고, 인터뷰는
기억에 의존해 부정확하다.

채택되지 않은 질의를 모아 두면 그것이 곧 다음 개선 작업의 우선순위가 된다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter

from redis.asyncio import Redis

from vai_contracts.authoring import FeedbackStats, PopupFeedback

KEY_PREFIX = "vai:feedback:"
MAX_RETAINED = 5000
"""테넌트당 보관 건수. 통계 산출에 충분하고, 영속 분석은 Wave 6의 DB가 맡는다."""

WORST_QUERY_LIMIT = 10


class FeedbackStore(ABC):
    @abstractmethod
    async def add(self, feedback: PopupFeedback) -> None: ...

    @abstractmethod
    async def stats(self, tenant_id: str) -> FeedbackStats: ...


def _summarize(items: list[PopupFeedback]) -> FeedbackStats:
    if not items:
        return FeedbackStats()
    accepted = sum(1 for item in items if item.accepted)
    rejected = Counter(item.query for item in items if not item.accepted)
    return FeedbackStats(
        total=len(items),
        accepted=accepted,
        acceptance_rate=round(accepted / len(items), 4),
        worst_queries=[query for query, _ in rejected.most_common(WORST_QUERY_LIMIT)],
    )


class RedisFeedbackStore(FeedbackStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def add(self, feedback: PopupFeedback) -> None:
        key = KEY_PREFIX + feedback.tenant_id
        await self._redis.lpush(key, feedback.model_dump_json().encode())
        await self._redis.ltrim(key, 0, MAX_RETAINED - 1)

    async def stats(self, tenant_id: str) -> FeedbackStats:
        raw_items = await self._redis.lrange(KEY_PREFIX + tenant_id, 0, MAX_RETAINED - 1)
        return _summarize([PopupFeedback.model_validate_json(item) for item in raw_items])


class InMemoryFeedbackStore(FeedbackStore):
    def __init__(self) -> None:
        self._items: dict[str, list[PopupFeedback]] = {}

    async def add(self, feedback: PopupFeedback) -> None:
        items = self._items.setdefault(feedback.tenant_id, [])
        items.insert(0, feedback)
        del items[MAX_RETAINED:]

    async def stats(self, tenant_id: str) -> FeedbackStats:
        return _summarize(self._items.get(tenant_id, []))
