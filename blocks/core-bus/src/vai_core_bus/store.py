"""세션 레지스트리 저장소.

세션은 수명이 짧고(통화·회의 단위) 조회가 잦아 Redis에 둔다. 영속 이력은
Wave 4에서 PostgreSQL(`session` 테이블)에 별도로 적재한다 — 레지스트리는
"지금 살아 있는 세션"만 책임진다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from redis.asyncio import Redis

from vai_contracts.session import Session, SessionState

KEY_PREFIX = "vai:session:"
TTL_SECONDS = 60 * 60 * 12
"""세션 레코드 TTL. 종료 이벤트를 놓쳐도 레지스트리가 무한정 늘지 않게 한다."""


class SessionStore(ABC):
    @abstractmethod
    async def save(self, session: Session) -> None: ...

    @abstractmethod
    async def get(self, session_id: str) -> Session | None: ...

    @abstractmethod
    async def close(self, session_id: str) -> Session | None: ...


class RedisSessionStore(SessionStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save(self, session: Session) -> None:
        await self._redis.set(
            KEY_PREFIX + session.session_id,
            session.model_dump_json().encode(),
            ex=TTL_SECONDS,
        )

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
