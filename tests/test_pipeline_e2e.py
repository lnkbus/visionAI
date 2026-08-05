"""파이프라인 통합 테스트 — Wave 2 DoD.

``audio.in`` 발행 한 번으로 AUD-VAD와 STT-CORE가 실제로 조립되어
``stt.delta``와 UI 이벤트까지 흐르는지 확인한다. Redis도 GPU도 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from tests.conftest import SAMPLE_RATE, pcm_silence, pcm_tone
from vai_aud_vad.adapters.energy import EnergyVadAdapter
from vai_aud_vad.worker import VadWorker
from vai_common.bus import InMemoryEventBus
from vai_contracts.events import AudioChunk, SttDelta
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.topics import Topic
from vai_stt_core.adapters.fake import DEFAULT_SCRIPT, FakeSTTAdapter
from vai_stt_core.worker import SttWorker

SESSION = "sess_test_0001"
TENANT = "tenant_test"


async def _collect(bus: InMemoryEventBus, topic: Topic, model: type, count: int, wait_s: float):
    """토픽에서 ``count``건을 모은다. 관찰용이라 별도 컨슈머 그룹을 쓴다."""
    collected = []

    async def run() -> None:
        async for delivery in bus.consume(topic, model, group="test-observer", consumer="obs"):
            collected.append(delivery.event)
            await delivery.ack()
            if len(collected) >= count:
                return

    with suppress(TimeoutError):
        await asyncio.wait_for(run(), timeout=wait_s)
    return collected


@pytest.fixture
async def pipeline(bus: InMemoryEventBus):
    """VAD + STT 워커를 띄우고 테스트 종료 시 정리한다."""
    vad = EnergyVadAdapter()
    await vad.initialize({})
    stt = FakeSTTAdapter()
    await stt.initialize("", {})

    vad_worker = VadWorker(bus, vad, group="vad", consumer="vad-1")
    stt_worker = SttWorker(bus, stt, group="stt", consumer="stt-1")

    # 인메모리 버스는 그룹 등록 이후의 메시지만 전달한다(Streams의 mkstream과 동일).
    # 발행보다 소비자 등록이 늦으면 첫 메시지를 잃으므로 미리 등록한다.
    bus.register_group(Topic.AUDIO_IN, "vad")
    bus.register_group(Topic.AUDIO_SEGMENT, "stt")
    bus.register_group(Topic.STT_DELTA, "test-observer")

    tasks = [asyncio.create_task(vad_worker.run()), asyncio.create_task(stt_worker.run())]
    try:
        yield bus
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def audio(pcm: bytes, seq: int) -> AudioChunk:
    return AudioChunk(
        session_id=SESSION,
        tenant_id=TENANT,
        seq=seq,
        channel=ChannelRole.CUSTOMER,
        sample_rate=SAMPLE_RATE,
        pcm=pcm,
        profile=SessionProfile.AICC,
    )


async def test_audio_in_reaches_stt_delta(pipeline: InMemoryEventBus) -> None:
    """마이크 한 마디 → 자막 한 줄. Wave 2가 증명해야 하는 단 하나의 경로."""
    collector = asyncio.create_task(_collect(pipeline, Topic.STT_DELTA, SttDelta, 1, 5.0))
    await asyncio.sleep(0)  # 관찰자가 먼저 붙을 기회를 준다

    await pipeline.publish(Topic.AUDIO_IN, audio(pcm_tone(600), 1))
    await pipeline.publish(Topic.AUDIO_IN, audio(pcm_silence(700), 2))

    deltas = await collector

    assert deltas, "STT Delta가 나오지 않았다"
    delta = deltas[0]
    assert delta.session_id == SESSION
    assert delta.tenant_id == TENANT, "테넌트가 파이프라인 끝까지 유지되어야 한다"
    assert delta.channel is ChannelRole.CUSTOMER
    assert delta.text == DEFAULT_SCRIPT[0]
    assert delta.confidence > 0


async def test_ui_channel_receives_caption(pipeline: InMemoryEventBus) -> None:
    """FLT-MICRO 투입 전까지는 STT가 UI로 직접 자막을 보낸다."""
    received: list[dict] = []

    async def watch() -> None:
        async for payload in pipeline.subscribe_ui(SESSION):
            received.append(payload)
            return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)

    await pipeline.publish(Topic.AUDIO_IN, audio(pcm_tone(600), 1))
    await pipeline.publish(Topic.AUDIO_IN, audio(pcm_silence(700), 2))

    with suppress(TimeoutError):
        await asyncio.wait_for(watcher, timeout=5.0)
    watcher.cancel()

    assert received, "UI 채널로 자막이 흐르지 않았다"
    message = received[0]
    assert message["event"] == "agent_assist_update"
    assert message["session_id"] == SESSION
    assert message["stt_result"]["text"] == DEFAULT_SCRIPT[0]


async def test_silence_alone_produces_no_transcript(pipeline: InMemoryEventBus) -> None:
    """무음에 STT를 태우면 GPU만 태운다."""
    collector = asyncio.create_task(_collect(pipeline, Topic.STT_DELTA, SttDelta, 1, 1.0))
    await asyncio.sleep(0)

    await pipeline.publish(Topic.AUDIO_IN, audio(pcm_silence(1500), 1))

    assert await collector == []
