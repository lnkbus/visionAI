"""토큰 사용량 계측 — 과금과 용량 관리의 입력.

기존 계측은 프로세스 메모리의 카운터 하나였다. 그 값으로는 문서가 말하는
두 가지를 **둘 다 할 수 없다**:

* **SaaS 과금** — 테넌트 구분이 없다. 총합만으로는 누구에게 청구할지 모른다.
* **온프렘 용량 관리** — 재기동하면 0이 되고, 게이트웨이를 두 개로 복제하면
  각자 다른 숫자를 들고 있다. 어느 쪽도 실제 사용량이 아니다.

그래서 Redis 카운터로 옮긴다. ``INCRBY``는 원자적이라 복제본이 몇 개든 같은
값에 더해지고, 재기동을 넘어 남는다. Redis가 없는 구성(단일 프로세스·CI)에서는
메모리 카운터로 내려가되 **그 사실을 응답에 담아** 숫자를 과신하지 않게 한다.

월 단위로 키를 나누는 이유는 청구 주기가 월이기 때문이고, 그 덕에 지난달
사용량이 이번 달 숫자에 섞이지 않는다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import UTC, datetime

from redis.asyncio import Redis

log = logging.getLogger(__name__)

KEY_PREFIX = "vai:llm-usage:"
FIELDS = ("calls", "prompt_tokens", "completion_tokens")

RETENTION_MONTHS = 25
"""사용량 키 보존 기간. 전년 동월 대비를 볼 수 있게 24개월 + 여유 1개월."""


def period(now: datetime | None = None) -> str:
    """청구 주기 식별자(``YYYY-MM``)."""
    return (now or datetime.now(UTC)).strftime("%Y-%m")


class UsageMeter(ABC):
    """테넌트·월별 토큰 계측."""

    durable: bool
    """재기동과 복제를 넘어 유지되는가. 응답에 실어 숫자의 성격을 밝힌다."""

    @abstractmethod
    async def record(
        self, tenant_id: str, *, prompt_tokens: int, completion_tokens: int
    ) -> None: ...

    @abstractmethod
    async def snapshot(self, month: str = "") -> dict[str, dict[str, int]]:
        """``{tenant_id: {calls, prompt_tokens, completion_tokens}}``."""


class MemoryUsageMeter(UsageMeter):
    """단일 프로세스용. 재기동하면 사라진다."""

    durable = False

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: dict.fromkeys(FIELDS, 0))
        )

    async def record(self, tenant_id: str, *, prompt_tokens: int, completion_tokens: int) -> None:
        bucket = self._data[period()][tenant_id or "_unknown"]
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens

    async def snapshot(self, month: str = "") -> dict[str, dict[str, int]]:
        return {tenant: dict(values) for tenant, values in self._data[month or period()].items()}


class RedisUsageMeter(UsageMeter):
    """복제본이 몇 개든 같은 값에 더해지고, 재기동을 넘어 남는다."""

    durable = True

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, month: str, tenant_id: str) -> str:
        return f"{KEY_PREFIX}{month}:{tenant_id}"

    async def record(self, tenant_id: str, *, prompt_tokens: int, completion_tokens: int) -> None:
        month = period()
        key = self._key(month, tenant_id or "_unknown")
        try:
            pipe = self._redis.pipeline()
            pipe.hincrby(key, "calls", 1)
            pipe.hincrby(key, "prompt_tokens", prompt_tokens)
            pipe.hincrby(key, "completion_tokens", completion_tokens)
            # 무한정 쌓이지 않게 만료를 건다. 청구가 끝난 과거 월까지 들고
            # 있을 이유가 없고, 그 판단을 사람이 기억하게 만들면 안 지켜진다.
            pipe.expire(key, RETENTION_MONTHS * 31 * 24 * 3600)
            await pipe.execute()
        except Exception:
            # 계측 실패로 상담을 멈추지 않는다. 다만 조용히 넘기지 않는다 —
            # 이 로그가 비면 청구 근거가 비는 것이다.
            log.warning("토큰 사용량 기록 실패", extra={"tenant_id": tenant_id}, exc_info=True)

    async def snapshot(self, month: str = "") -> dict[str, dict[str, int]]:
        target = month or period()
        prefix = f"{KEY_PREFIX}{target}:"
        result: dict[str, dict[str, int]] = {}
        async for raw_key in self._redis.scan_iter(match=f"{prefix}*"):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            values = await self._redis.hgetall(key)
            result[key[len(prefix) :]] = {
                field: int(values.get(field.encode(), values.get(field, 0)) or 0)
                for field in FIELDS
            }
        return result
