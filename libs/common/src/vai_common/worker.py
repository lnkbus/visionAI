"""컨슈머 워커 골격.

VAD·STT·필터처럼 "토픽을 읽어 처리하고 다음 토픽으로 넘기는" 블록은 모두 같은
모양이다. 소비 루프·에러 처리·지연 계측을 여기 모아 두고, 각 블록은
:meth:`BlockWorker.handle`만 구현한다.

지연 계측을 골격에 넣은 이유: 사양서의 단계별 예산(필터 10ms, 검색 100ms,
리랭킹 80ms)은 나중에 측정하면 이미 늦다. 블록마다 예산을 선언하게 하고
초과 시 경고를 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from vai_common.bus import EventBus
from vai_contracts.events import BaseEvent
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)

EventT = TypeVar("EventT", bound=BaseEvent)


class BlockWorker(ABC, Generic[EventT]):
    """단일 토픽 소비 워커.

    하위 클래스는 ``source_topic``/``source_model``/``block_id``를 선언하고
    :meth:`handle`을 구현한다.
    """

    block_id: str
    source_topic: Topic
    source_model: type[EventT]
    latency_budget_ms: float | None = None
    """초과 시 경고 로그를 남길 처리 지연 예산. None이면 계측만 한다."""

    def __init__(self, bus: EventBus, *, group: str, consumer: str) -> None:
        self.bus = bus
        self.group = group
        self.consumer = consumer
        self._stopping = asyncio.Event()

    @abstractmethod
    async def handle(self, event: EventT) -> None:
        """이벤트 1건 처리. 예외를 던지면 메시지는 ack되지 않고 재전달된다."""

    async def run(self) -> None:
        log.info(
            "워커 시작",
            extra={"block": self.block_id, "topic": self.source_topic.value, "group": self.group},
        )
        stream = self.bus.consume(
            self.source_topic, self.source_model, group=self.group, consumer=self.consumer
        )
        async for delivery in stream:
            if self._stopping.is_set():
                break
            started = time.perf_counter()
            try:
                await self.handle(delivery.event)
            except Exception:
                # ack하지 않는다 → 펜딩으로 남아 재처리 대상이 된다.
                log.exception(
                    "이벤트 처리 실패",
                    extra={
                        "block": self.block_id,
                        "session_id": delivery.event.session_id,
                        "message_id": delivery.message_id,
                    },
                )
                continue
            await delivery.ack()
            self._observe(time.perf_counter() - started, delivery.event)

    def _observe(self, elapsed_s: float, event: EventT) -> None:
        elapsed_ms = elapsed_s * 1000
        if self.latency_budget_ms is not None and elapsed_ms > self.latency_budget_ms:
            log.warning(
                "지연 예산 초과",
                extra={
                    "block": self.block_id,
                    "session_id": event.session_id,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "budget_ms": self.latency_budget_ms,
                },
            )

    def stop(self) -> None:
        self._stopping.set()
