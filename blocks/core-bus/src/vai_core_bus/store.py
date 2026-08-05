"""세션 레지스트리 저장소.

세션은 수명이 짧고(통화·회의 단위) 조회가 잦아 Redis에 둔다. 레지스트리는
**"지금 살아 있는 세션"만 책임진다** — TTL로 스스로 정리되고, 남겨야 할 것은
다른 곳에 있다: 무슨 일이 있었는지는 감사 로그(CORE-SEC), 무슨 이야기였는지는
요약(LLM-SUM)이다. 세 곳의 수명이 같아야 할 이유가 없다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from redis.asyncio import Redis

from vai_contracts.session import Session, SessionState

KEY_PREFIX = "vai:session:"
ACTIVE_KEY = "vai:session:active"
"""열려 있는 세션의 정렬 집합(점수=생성 시각). 동시 채널 수를 세는 근거다.

세션 키를 SCAN 하지 않는 이유: 키가 수만 개인 운영 환경에서 SCAN은 느리고,
상담 인입 경로에서 그 비용을 치를 수 없다."""

TTL_SECONDS = 60 * 60 * 12
"""세션 레코드 TTL. 종료 이벤트를 놓쳐도 레지스트리가 무한정 늘지 않게 한다."""


class SessionStore(ABC):
    @abstractmethod
    async def save(self, session: Session) -> None: ...

    @abstractmethod
    async def get(self, session_id: str) -> Session | None: ...

    @abstractmethod
    async def close(self, session_id: str) -> Session | None: ...

    @abstractmethod
    async def active_count(self) -> int:
        """열려 있는 세션 수. 라이선스 동시 채널 한도의 분모다."""


class RedisSessionStore(SessionStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save(self, session: Session) -> None:
        pipe = self._redis.pipeline()
        pipe.set(
            KEY_PREFIX + session.session_id,
            session.model_dump_json().encode(),
            ex=TTL_SECONDS,
        )
        if session.state is SessionState.CLOSED:
            pipe.zrem(ACTIVE_KEY, session.session_id)
        else:
            pipe.zadd(ACTIVE_KEY, {session.session_id: session.created_at.timestamp()})
        await pipe.execute()

    async def get(self, session_id: str) -> Session | None:
        raw = await self._redis.get(KEY_PREFIX + session_id)
        return Session.model_validate_json(raw) if raw else None

    async def close(self, session_id: str) -> Session | None:
        session = await self.get(session_id)
        if session is None or session.state is SessionState.CLOSED:
            return session
        session.state = SessionState.CLOSED
        session.closed_at = datetime.now(UTC)
        await self.save(session)
        return session

    async def active_count(self) -> int:
        """열려 있는 세션 수.

        세는 김에 TTL을 넘긴 항목을 걷어낸다. 종료 이벤트를 놓친 세션이
        집합에 영원히 남으면 채널이 조금씩 새어 나가고, 결국 라이선스 안에서
        상담을 못 받는 상태가 된다 — 원인을 찾기 가장 어려운 종류의 장애다.
        """
        cutoff = datetime.now(UTC).timestamp() - TTL_SECONDS
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(ACTIVE_KEY, "-inf", cutoff)
        pipe.zcard(ACTIVE_KEY)
        results = await pipe.execute()
        return int(results[-1])


class InMemorySessionStore(SessionStore):
    """테스트·단일 프로세스 실행용."""

    def __init__(self) -> None:
        self._data: dict[str, Session] = {}

    async def save(self, session: Session) -> None:
        self._data[session.session_id] = session

    async def get(self, session_id: str) -> Session | None:
        return self._data.get(session_id)

    async def close(self, session_id: str) -> Session | None:
        session = self._data.get(session_id)
        if session is None or session.state is SessionState.CLOSED:
            return session
        session.state = SessionState.CLOSED
        session.closed_at = datetime.now(UTC)
        return session

    async def active_count(self) -> int:
        return sum(1 for s in self._data.values() if s.state is not SessionState.CLOSED)
