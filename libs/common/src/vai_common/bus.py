"""이벤트 버스 클라이언트 — 블록 조립의 배선.

두 경로를 하나의 인터페이스로 제공한다.

* ``publish``/``consume`` — Redis **Streams**. 컨슈머 그룹으로 워커 수평 확장이
  되고, ack 전까지 메시지가 보존되므로 워커가 죽어도 유실되지 않는다.
* ``publish_ui``/``subscribe_ui`` — Redis **Pub/Sub**. 접속 중인 화면으로의
  팬아웃 전용이다. 화면이 없으면 버려도 되는 데이터라 내구성이 필요 없고,
  Streams보다 지연이 낮아 사양서의 UI 렌더 예산(<50ms)에 유리하다.

테스트·CI용 :class:`InMemoryEventBus`가 같은 인터페이스를 구현하므로
Redis 없이도 파이프라인 전체를 통합 테스트할 수 있다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel
from redis.asyncio import Redis

from vai_contracts.events import BaseEvent
from vai_contracts.topics import Topic, stream, ui_channel

log = logging.getLogger(__name__)

EventT = TypeVar("EventT", bound=BaseEvent)

DATA_FIELD = "data"
MAX_STREAM_LEN = 100_000
"""스트림당 보존 상한(근사). 오디오 청크가 흐르므로 무제한 성장은 곧 OOM이다."""


@dataclass
class Delivery(Generic[EventT]):
    """소비된 메시지 1건. 처리 성공 후 :meth:`ack`을 호출해야 재전달되지 않는다."""

    message_id: str
    event: EventT
    _bus: EventBus
    _topic: Topic
    _group: str

    async def ack(self) -> None:
        await self._bus.ack(self._topic, self._group, self.message_id)


class EventBus(ABC):
    @abstractmethod
    async def publish(self, topic: Topic, event: BaseEvent) -> None: ...

    @abstractmethod
    def consume(
        self,
        topic: Topic,
        model: type[EventT],
        *,
        group: str,
        consumer: str,
        block_ms: int = 2000,
    ) -> AsyncIterator[Delivery[EventT]]: ...

    @abstractmethod
    async def ack(self, topic: Topic, group: str, message_id: str) -> None: ...

    @abstractmethod
    async def publish_ui(self, session_id: str, payload: BaseModel) -> None: ...

    @abstractmethod
    def subscribe_ui(self, session_id: str) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None:  # pragma: no cover - 기본 구현은 no-op
        return None


class RedisEventBus(EventBus):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._ensured: set[tuple[Topic, str]] = set()

    @classmethod
    def from_url(cls, url: str) -> RedisEventBus:
        return cls(Redis.from_url(url, decode_responses=False))

    @property
    def redis(self) -> Redis:
        """같은 Redis를 쓰는 블록 로컬 저장소(세션 레지스트리 등)에 연결을 재사용한다."""
        return self._redis

    async def publish(self, topic: Topic, event: BaseEvent) -> None:
        await self._redis.xadd(
            stream(topic),
            {DATA_FIELD: event.model_dump_json().encode()},
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )

    async def _ensure_group(self, topic: Topic, group: str) -> None:
        if (topic, group) in self._ensured:
            return
        with contextlib.suppress(Exception):
            # BUSYGROUP = 이미 존재. mkstream으로 첫 소비자가 생산자보다 먼저 떠도 동작한다.
            await self._redis.xgroup_create(stream(topic), group, id="0", mkstream=True)
        self._ensured.add((topic, group))

    async def consume(
        self,
        topic: Topic,
        model: type[EventT],
        *,
        group: str,
        consumer: str,
        block_ms: int = 2000,
    ) -> AsyncIterator[Delivery[EventT]]:
        await self._ensure_group(topic, group)
        key = stream(topic)
        while True:
            # redis-py의 반환 타입은 명령마다 달라 Any로 느슨하게 선언되어 있다.
            # XREADGROUP의 실제 형태를 여기서 한 번 고정한다.
            batches: list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]] = cast(
                "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
                await self._redis.xreadgroup(group, consumer, {key: ">"}, count=16, block=block_ms),
            )
            if not batches:
                continue
            for _stream_key, messages in batches:
                for message_id, fields in messages:
                    raw = fields.get(DATA_FIELD.encode())
                    mid = message_id.decode()
                    if raw is None:
                        log.warning("payload 없는 메시지 폐기", extra={"message_id": mid})
                        await self.ack(topic, group, mid)
                        continue
                    try:
                        event = model.model_validate_json(raw)
                    except ValueError:
                        # 스키마 불일치는 재시도해도 낫지 않는다. ack하고 넘긴다.
                        log.exception("이벤트 파싱 실패", extra={"topic": topic.value})
                        await self.ack(topic, group, mid)
                        continue
                    yield Delivery(mid, event, self, topic, group)

    async def ack(self, topic: Topic, group: str, message_id: str) -> None:
        await self._redis.xack(stream(topic), group, message_id)

    async def publish_ui(self, session_id: str, payload: BaseModel) -> None:
        await self._redis.publish(ui_channel(session_id), payload.model_dump_json().encode())

    async def subscribe_ui(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(ui_channel(session_id))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                yield cast("dict[str, Any]", json.loads(message["data"]))
        finally:
            await pubsub.unsubscribe(ui_channel(session_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        await self._redis.aclose()


class InMemoryEventBus(EventBus):
    """단일 프로세스용 버스. 테스트와 compose 없는 로컬 실행에 쓴다.

    컨슈머 그룹별로 독립 큐를 두므로 서로 다른 그룹은 같은 메시지를 각각 받고,
    같은 그룹의 여러 소비자는 큐를 나눠 갖는다 — Redis Streams와 같은 의미다.
    """

    def __init__(self) -> None:
        self._queues: dict[tuple[Topic, str], asyncio.Queue[BaseEvent]] = {}
        self._groups: dict[Topic, set[str]] = defaultdict(set)
        self._ui: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._counter = 0

    def register_group(self, topic: Topic, group: str) -> None:
        """소비 시작 전에 그룹을 등록해 둔다.

        Streams의 ``id="0"`` + mkstream과 같은 역할이다. 등록 전에 발행된
        메시지는 그 그룹에 전달되지 않으므로, 테스트에서는 소비자를 먼저 띄우거나
        이 메서드를 먼저 호출해야 한다.
        """
        self._groups[topic].add(group)
        self._queues.setdefault((topic, group), asyncio.Queue())

    async def publish(self, topic: Topic, event: BaseEvent) -> None:
        for group in self._groups[topic]:
            # 소비자가 원본을 수정해도 다른 그룹에 번지지 않도록 복사본을 넣는다.
            await self._queues[(topic, group)].put(event.model_copy(deep=True))

    async def consume(
        self,
        topic: Topic,
        model: type[EventT],
        *,
        group: str,
        consumer: str,
        block_ms: int = 2000,
    ) -> AsyncIterator[Delivery[EventT]]:
        self.register_group(topic, group)
        queue = self._queues[(topic, group)]
        while True:
            event = await queue.get()
            self._counter += 1
            parsed = model.model_validate(event.model_dump())
            yield Delivery(str(self._counter), parsed, self, topic, group)

    async def ack(self, topic: Topic, group: str, message_id: str) -> None:
        return None

    async def publish_ui(self, session_id: str, payload: BaseModel) -> None:
        data = payload.model_dump(mode="json")
        for queue in self._ui[session_id]:
            await queue.put(data)

    async def subscribe_ui(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._ui[session_id].append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._ui[session_id].remove(queue)


def build_bus(redis_url: str | None) -> EventBus:
    """설정에 따라 버스 구현을 고른다. ``redis_url``이 비면 인메모리."""
    if not redis_url:
        return InMemoryEventBus()
    return RedisEventBus.from_url(redis_url)
