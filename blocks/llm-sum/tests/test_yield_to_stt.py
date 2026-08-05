"""요약이 실시간 인식에 양보하는가.

한 장비에 STT와 요약 sLLM을 함께 올리면 메모리를 다툰다. 회의 중 자막이
끊기는 것과 회의록이 3분 늦는 것은 비용이 다르므로 순서를 정한다 —
**STT가 쓰는 동안 요약은 기다린다.**

여기서 확인하는 것은 세 가지다: 기다리는가, 기다린다는 사실이 저장되는가
(그래야 화면에 보인다), 그리고 **영영 기다리지는 않는가**.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from vai_common.bus import InMemoryEventBus
from vai_common.resource import MemoryBusyRegistry, Yield
from vai_contracts.events import FilterResult, SessionClosed
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import SummaryStatus
from vai_contracts.topics import Topic
from vai_llm_sum.store import InMemorySummaryStore
from vai_llm_sum.worker import SummaryWorker, TranscriptBuffer, TranscriptCollector

SESSION = "sess_yield_0001"
TENANT = "t1"
MEETING_JSON = '{"title": "주간회의", "agenda": ["일정"], "decisions": [], "action_items": []}'


async def _responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
    return MEETING_JSON


@pytest.fixture
def bus() -> InMemoryEventBus:
    bus = InMemoryEventBus()
    bus.register_group(Topic.FILTER_CLEAN, "transcript")
    bus.register_group(Topic.SESSION_CLOSED, "summary")
    return bus


class RecordingStore(InMemorySummaryStore):
    """상태 전이를 순서대로 남긴다.

    최종 상태만 보면 WAITING 을 지나갔는지 알 수 없고, 그러면 화면에 무엇이
    보였는지도 알 수 없다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.states: list[SummaryStatus] = []

    async def put(self, summary):  # type: ignore[no-untyped-def]
        self.states.append(summary.status)
        await super().put(summary)


async def _run(bus: InMemoryEventBus, yielder: Yield | None, store: RecordingStore):
    buffer = TranscriptBuffer()
    collector = TranscriptCollector(bus, buffer, group="transcript", consumer="c1")
    worker = SummaryWorker(
        bus, buffer, _responder, store, group="summary", consumer="w1", yield_to_stt=yielder
    )
    tasks = [asyncio.create_task(collector.run()), asyncio.create_task(worker.run())]
    try:
        await bus.publish(
            Topic.FILTER_CLEAN,
            FilterResult(
                session_id=SESSION,
                tenant_id=TENANT,
                channel=ChannelRole.CUSTOMER,
                clean_text="일정 공유합니다",
                is_final=True,
            ),
        )
        await asyncio.sleep(0.05)
        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=SessionProfile.MEETING),
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            summary = await store.get(SESSION)
            if summary and summary.status in (SummaryStatus.READY, SummaryStatus.FAILED):
                return summary
        return await store.get(SESSION)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


async def test_인식_중이면_대기_상태를_먼저_저장한다(bus: InMemoryEventBus) -> None:
    """대기 사실이 저장되지 않으면 화면은 아무 말도 못 한다."""
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=60)
    store = RecordingStore()

    async def release() -> None:
        await asyncio.sleep(0.1)
        await registry.clear()

    releaser = asyncio.create_task(release())
    summary = await _run(bus, Yield(registry, poll_interval_s=0.01, max_wait_s=5.0), store)
    await releaser

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert SummaryStatus.WAITING in store.states
    assert store.states.index(SummaryStatus.WAITING) < store.states.index(SummaryStatus.RUNNING)
    assert summary.waited_for_stt_ms > 0
    assert not summary.yield_gave_up


async def test_인식이_한가하면_대기_없이_바로_요약한다(bus: InMemoryEventBus) -> None:
    store = RecordingStore()

    summary = await _run(bus, Yield(MemoryBusyRegistry(), poll_interval_s=0.01), store)

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert summary.waited_for_stt_ms == 0


async def test_인식이_안_끝나도_상한을_넘기면_요약을_만든다(bus: InMemoryEventBus) -> None:
    """가장 중요한 시험.

    통화가 끊이지 않는 상담센터에서 "한가해질 때"는 오지 않는다. 여기서
    계속 기다리면 회의록이 늦는 게 아니라 **안 나온다**.
    """
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=600)
    store = RecordingStore()

    summary = await _run(bus, Yield(registry, poll_interval_s=0.01, max_wait_s=0.05), store)

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert summary.yield_gave_up
    assert summary.meeting is not None


async def test_양보를_끄면_바쁜_중에도_바로_돈다(bus: InMemoryEventBus) -> None:
    """GPU가 넉넉한 서버에서 이 직렬화는 손해다."""
    registry = MemoryBusyRegistry()
    await registry.mark_busy(ttl_s=600)
    store = RecordingStore()

    summary = await _run(bus, Yield(registry, enabled=False), store)

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert SummaryStatus.WAITING not in store.states


async def test_양보_설정이_없으면_이전과_똑같이_동작한다(bus: InMemoryEventBus) -> None:
    """기존 배포·시험이 이 인자를 모른다. 없으면 그냥 돌아야 한다."""
    store = RecordingStore()

    summary = await _run(bus, None, store)

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert SummaryStatus.WAITING not in store.states
