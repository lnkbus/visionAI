"""컨슈머 워커 골격.

VAD·STT·필터처럼 "토픽을 읽어 처리하고 다음 토픽으로 넘기는" 블록은 모두 같은
모양이다. 소비 루프·에러 처리·지연 계측을 여기 모아 두고, 각 블록은
:meth:`BlockWorker.handle`만 구현한다.

지연 계측을 골격에 넣은 이유: 사양서의 단계별 예산(필터 10ms, 검색 100ms,
리랭킹 80ms)은 나중에 측정하면 이미 늦다. 블록마다 예산을 선언하게 하고
초과 시 경고를 남긴다.

**세션 상태 해제도 골격에 넣는다.** 워커가 세션별 버퍼를 들고 있는데 세션이
끝날 때 비우지 않으면, 24시간 돌아가는 온프렘에서 메모리가 조용히 늘다가
어느 날 죽는다. 블록마다 종료 이벤트 구독 루프를 따로 쓰면 하나쯤 빠뜨리게
되고, 빠뜨렸다는 사실은 몇 달 뒤에 드러난다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from vai_common.bus import EventBus
from vai_contracts.events import BaseEvent, SessionClosed
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

    def release_session(self, session_id: str) -> None:
        """세션이 끝났을 때 이 워커가 들고 있던 상태를 버린다.

        기본은 아무것도 하지 않는다 — 상태가 없는 워커가 대부분이다.
        세션별 버퍼(발화 창, 화자 중심, 시퀀스 카운터)를 가진 워커는
        재정의하고, :class:`SessionReaper`에 등록해야 실제로 불린다.
        **정의만 하고 등록하지 않으면 조용히 새는 상태가 된다.**
        """
        return None

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


class SessionReaper:
    """세션 종료 이벤트를 받아 워커들의 세션 상태를 해제한다.

    **컨슈머 그룹을 블록마다 따로 쓴다.** ``session.closed``는 요약 블록도
    소비하므로 그룹을 공유하면 둘 중 하나만 메시지를 받고, 못 받은 쪽은
    영원히 해제되지 않는다 — 게다가 그 사실이 오류로 드러나지 않는다.
    """

    def __init__(
        self,
        bus: EventBus,
        workers: list[BlockWorker[Any]],
        *,
        group: str,
        consumer: str = "reaper-1",
    ) -> None:
        self._bus = bus
        self._workers = workers
        self._group = group
        self._consumer = consumer
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        stream = self._bus.consume(
            Topic.SESSION_CLOSED,
            SessionClosed,
            group=self._group,
            consumer=self._consumer,
        )
        async for delivery in stream:
            if self._stopping.is_set():
                break
            for worker in self._workers:
                # 한 워커의 해제가 실패해도 나머지는 비운다. 해제 실패로
                # 재처리 루프에 빠지면 그게 더 큰 누수다.
                try:
                    worker.release_session(delivery.event.session_id)
                except Exception:
                    log.exception(
                        "세션 상태 해제 실패",
                        extra={
                            "block": worker.block_id,
                            "session_id": delivery.event.session_id,
                        },
                    )
            await delivery.ack()

    def stop(self) -> None:
        self._stopping.set()
