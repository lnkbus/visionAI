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

    @abstractmethod
    async def reserve(self, session: Session, limit: int | None) -> int | None:
        """한도를 확인하고 **같은 동작 안에서** 세션을 등록한다.

        성공하면 ``None``, 한도 초과면 그 시점의 활성 세션 수를 돌려준다
        (거부 사유를 감사에 남길 때 쓴다).

        세고 나서 따로 저장하면 그 사이에 다른 요청이 끼어든다. 콜센터의
        인입은 정확히 그런 모양이라 — 착신이 몰리는 순간에만 한도가 뚫린다 —
        재현도 어렵고, 그동안 라이선스는 장식이 된다.
        """


RESERVE_SCRIPT = """
local active_key, session_key = KEYS[1], KEYS[2]
local cutoff, limit = ARGV[1], tonumber(ARGV[2])
local member, score, payload, ttl = ARGV[3], ARGV[4], ARGV[5], tonumber(ARGV[6])

redis.call('ZREMRANGEBYSCORE', active_key, '-inf', cutoff)
if limit >= 0 then
  local current = redis.call('ZCARD', active_key)
  if current >= limit then
    return current
  end
end
redis.call('ZADD', active_key, score, member)
redis.call('SET', session_key, payload, 'EX', ttl)
return -1
"""
"""한도 검사와 등록을 한 번에 하는 스크립트.

Redis는 스크립트를 단일 스레드에서 통째로 실행한다 — 파이프라인과 달리
중간에 다른 클라이언트가 끼어들 수 없다. 복제본이 몇 개든 같은 결과가 된다.
"""


class RedisSessionStore(SessionStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._reserve = redis.register_script(RESERVE_SCRIPT)

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

    async def reserve(self, session: Session, limit: int | None) -> int | None:
        cutoff = datetime.now(UTC).timestamp() - TTL_SECONDS
        outcome = int(
            await self._reserve(
                keys=[ACTIVE_KEY, KEY_PREFIX + session.session_id],
                args=[
                    cutoff,
                    -1 if limit is None else limit,
                    session.session_id,
                    session.created_at.timestamp(),
                    session.model_dump_json(),
                    TTL_SECONDS,
                ],
            )
        )
        return None if outcome < 0 else outcome

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

    async def reserve(self, session: Session, limit: int | None) -> int | None:
        # 이 사이에 await이 없어야 원자적이다 — 단일 이벤트 루프에서는
        # 그것으로 충분하다. 여러 프로세스가 붙는 구성은 Redis 저장소를 쓴다.
        active = sum(1 for s in self._data.values() if s.state is not SessionState.CLOSED)
        if limit is not None and active >= limit:
            return active
        self._data[session.session_id] = session
        return None

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
