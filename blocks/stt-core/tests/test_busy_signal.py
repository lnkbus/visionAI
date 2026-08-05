"""인식 중임을 알리는가.

이 표시가 없으면 요약(LLM-SUM)이 양보할 대상을 못 본다. 그러면 16GB 단일
장비에서 둘이 메모리를 다투고, 그때 먼저 무너지는 것이 실시간 자막이다.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from vai_common.bus import InMemoryEventBus
from vai_common.resource import Heartbeat, MemoryBusyRegistry
from vai_contracts.events import AudioSegment
from vai_contracts.session import ChannelRole, SessionProfile
from vai_stt_core.adapters.base import BaseSTTAdapter
from vai_stt_core.worker import SttWorker

SESSION = "sess_busy_0001"


class StubAdapter(BaseSTTAdapter):
    name = "stub"

    async def initialize(self, model_path: str, config: dict[str, Any]) -> None:
        return None

    async def transcribe_stream(
        self, pcm: bytes, sample_rate: int, *, hint: str = ""
    ) -> AsyncGenerator[dict[str, Any], None]:
        yield {"text": "안녕하세요", "is_final": True, "confidence": 0.9}

    async def close(self) -> None:
        return None


def segment() -> AudioSegment:
    return AudioSegment(
        session_id=SESSION,
        tenant_id="t1",
        channel=ChannelRole.CUSTOMER,
        profile=SessionProfile.MEETING,
        pcm=b"\x00\x00" * 160,
        sample_rate=16000,
        start_ms=0,
        duration_ms=20,
        is_final=True,
    )


async def test_인식하면_바쁨_표시가_선다() -> None:
    registry = MemoryBusyRegistry()
    worker = SttWorker(
        InMemoryEventBus(),
        StubAdapter(),
        group="g",
        consumer="c",
        heartbeat=Heartbeat(registry),
    )

    await worker.handle(segment())

    assert await registry.is_busy()


async def test_하트비트가_없어도_인식은_돌아간다() -> None:
    """양보 기능이 꺼진 배포에서 STT가 멈추면 안 된다."""
    bus = InMemoryEventBus()
    worker = SttWorker(bus, StubAdapter(), group="g", consumer="c")

    await worker.handle(segment())  # 예외 없이 끝나면 통과다


async def test_표시를_끄면_서지_않는다() -> None:
    registry = MemoryBusyRegistry()
    worker = SttWorker(
        InMemoryEventBus(),
        StubAdapter(),
        group="g",
        consumer="c",
        heartbeat=Heartbeat(registry, enabled=False),
    )

    await worker.handle(segment())

    assert not await registry.is_busy()
