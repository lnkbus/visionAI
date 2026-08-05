"""GPU·메모리 양보 — 실시간 경로가 배치 경로보다 먼저다.

한 장비에 STT와 요약 sLLM을 함께 올리면 둘이 메모리를 다툰다. 16GB 노트북에서는
스왑이 나고, 그러면 **먼저 무너지는 것이 실시간 자막**이다. 회의 중에 자막이
끊기는 것과 회의록이 3분 늦게 나오는 것은 비용이 다르다.

그래서 순서를 정한다: **STT가 쓰는 동안 요약은 기다린다.**

세 가지를 지킨다:

1. **요약이 영원히 밀리면 안 된다.** 상담센터처럼 통화가 끊이지 않는 곳에서는
   "STT가 한가해질 때"가 영영 오지 않는다. 최대 대기 시간을 두고, 넘으면
   **양보를 포기하고 진행한다** — 늦은 회의록이 없는 회의록보다 낫다.
2. **기다리는 중이라는 사실이 보여야 한다.** 화면에 아무 표시가 없으면 사용자는
   요약이 실패했다고 생각하고, 그때 할 수 있는 일이 없다.
3. **끌 수 있어야 한다.** GPU가 충분한 서버에서 이 직렬화는 손해다.
   ``enabled=False``면 즉시 진행한다.

바쁨 표시는 **짧은 TTL의 하트비트**다. 프로세스가 죽어도 표시가 남지 않는다 —
남으면 요약이 영원히 대기하고, 그건 되돌리기 어려운 종류의 정지다.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from redis.asyncio import Redis

BUSY_KEY = "vai:busy:stt"
"""STT가 작업 중임을 알리는 키. 테넌트로 나누지 않는다 — 다투는 것은 장비의
GPU·메모리이지 테넌트가 아니다."""

BUSY_TTL_S = 20
"""하트비트 수명. 프로세스가 죽으면 이 시간 안에 저절로 풀린다.

짧게 잡으면 인식 사이 빈틈마다 요약이 끼어들고, 길게 잡으면 죽은 프로세스의
표시가 오래 남는다. 발화 간격(수 초)보다 넉넉하고 사람이 기다릴 수 있는
시간보다 짧게."""

DEFAULT_MAX_WAIT_S = 180.0
"""양보 상한. 넘으면 포기하고 진행한다.

회의록은 회의 직후에 필요하다. 3분을 넘겨 기다리는 것은 '나중에'가 아니라
'안 나옴'으로 읽힌다."""

POLL_INTERVAL_S = 2.0


class BusyRegistry(ABC):
    """누가 장비를 쓰고 있는가."""

    @abstractmethod
    async def mark_busy(self, ttl_s: int = BUSY_TTL_S) -> None:
        """작업 중임을 알린다. TTL이 있어 갱신하지 않으면 저절로 풀린다."""

    @abstractmethod
    async def is_busy(self) -> bool: ...

    @abstractmethod
    async def clear(self) -> None:
        """즉시 푼다. 작업이 끝났을 때 TTL을 기다리지 않게."""


class MemoryBusyRegistry(BusyRegistry):
    """단일 프로세스용. 프로세스가 다르면 서로를 못 본다 —
    compose·쿠버네티스에서는 Redis 구현을 쓴다."""

    def __init__(self) -> None:
        self._until = 0.0

    async def mark_busy(self, ttl_s: int = BUSY_TTL_S) -> None:
        self._until = time.monotonic() + ttl_s

    async def is_busy(self) -> bool:
        return time.monotonic() < self._until

    async def clear(self) -> None:
        self._until = 0.0


class RedisBusyRegistry(BusyRegistry):
    """프로세스·노드를 넘어 보인다."""

    def __init__(self, redis: Redis, key: str = BUSY_KEY) -> None:
        self._redis = redis
        self._key = key

    async def mark_busy(self, ttl_s: int = BUSY_TTL_S) -> None:
        await self._redis.set(self._key, b"1", ex=ttl_s)

    async def is_busy(self) -> bool:
        return bool(await self._redis.exists(self._key))

    async def clear(self) -> None:
        await self._redis.delete(self._key)


@dataclass
class Yield:
    """배치 경로가 실시간 경로에 양보한다."""

    registry: BusyRegistry
    enabled: bool = True
    max_wait_s: float = DEFAULT_MAX_WAIT_S
    poll_interval_s: float = POLL_INTERVAL_S

    waited_s: float = 0.0
    """마지막 대기 시간. 화면·로그가 이 값을 쓴다."""

    gave_up: bool = False
    """상한을 넘겨 양보를 포기했는가. **실패가 아니라 기록이다** —
    자주 켜지면 장비가 모자란다는 뜻이고, 그건 증설 근거가 된다."""

    async def wait_for_turn(self) -> float:
        """차례를 기다린다. 기다린 시간(초)을 돌려준다.

        기다리지 않아도 되면 0.0. 상한을 넘기면 :attr:`gave_up`을 세우고
        그대로 진행한다 — 늦은 회의록이 없는 회의록보다 낫다.
        """
        self.waited_s = 0.0
        self.gave_up = False
        if not self.enabled:
            return 0.0

        started = time.monotonic()
        while await self.registry.is_busy():
            elapsed = time.monotonic() - started
            if elapsed >= self.max_wait_s:
                self.waited_s = elapsed
                self.gave_up = True
                return elapsed
            await asyncio.sleep(min(self.poll_interval_s, self.max_wait_s - elapsed))
        self.waited_s = time.monotonic() - started
        return self.waited_s


@dataclass
class Heartbeat:
    """실시간 경로가 "쓰는 중"을 알린다.

    작업마다 갱신한다. 매번 Redis를 때리지 않도록 **갱신 간격**을 둔다 —
    발화 하나에 한 번씩 SET을 보내면 그 자체가 지연이 된다.
    """

    registry: BusyRegistry
    enabled: bool = True
    refresh_interval_s: float = BUSY_TTL_S / 3
    ttl_s: int = BUSY_TTL_S

    _last: float = 0.0

    async def touch(self) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last < self.refresh_interval_s:
            return
        self._last = now
        await self.registry.mark_busy(self.ttl_s)
