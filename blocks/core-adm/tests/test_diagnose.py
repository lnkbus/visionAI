"""블록 인터페이스 시험이 실제로 무엇을 말하는가.

이 화면은 "블록이 도는가"를 판단하는 근거가 된다. 근거가 틀리면 두 방향으로
비싸다 — 안 도는 것을 돈다고 하거나(현장에서 드러남), 도는 것을 안 돈다고
해서 멀쩡한 시스템을 뜯는다.

그래서 통과와 실패를 **양쪽 다** 시험한다. 항상 통과하는 진단은 없는 진단보다
나쁘고, 항상 실패하는 진단은 곧 무시된다.
"""

from __future__ import annotations

import asyncio

import pytest

from vai_common.bus import InMemoryEventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import BaseEvent, FilterResult, SttDelta
from vai_contracts.session import ChannelRole
from vai_contracts.topics import Topic
from vai_core_adm.diagnose import (
    DIAG_PREFIX,
    DIRECT_CALLS,
    ROUND_TRIPS,
    SAMPLE_TEXT,
    _summarize,
    diag_session,
    probes,
    run_round_trip,
)


class _Echo(BlockWorker[SttDelta]):
    """stt.delta 를 받아 filter.clean 을 내는 최소 워커 — FLT-MICRO 자리."""

    block_id = "FAKE-FLT"
    source_topic = Topic.STT_DELTA
    source_model = SttDelta

    def __init__(self, bus: InMemoryEventBus, *, delay_s: float = 0.0) -> None:
        super().__init__(bus, group="fake-flt", consumer="fake-1")
        self._bus = bus
        self._delay = delay_s

    async def handle(self, event: SttDelta) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        await self._bus.publish(
            Topic.FILTER_CLEAN,
            FilterResult(
                session_id=event.session_id,
                tenant_id=event.tenant_id,
                channel=ChannelRole.CUSTOMER,
                clean_text=f"[처리됨] {event.text}",
                is_final=True,
            ),
        )


async def _with_worker(worker: BlockWorker[SttDelta], coro):  # type: ignore[no-untyped-def]
    task = asyncio.create_task(worker.run())
    try:
        return await coro
    finally:
        worker.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── 왕복 ────────────────────────────────────────────────────────────────────


async def test_round_trip_reports_the_answer_that_came_back() -> None:
    bus = InMemoryEventBus()
    trip = ROUND_TRIPS["FLT-MICRO"]
    result = await _with_worker(
        _Echo(bus),
        run_round_trip(bus, trip, tenant_id="t1", text="여기 이 문장", timeout_s=3.0),
    )

    assert result.ok
    assert result.block_id == "FLT-MICRO"
    assert result.payload["clean_text"] == "[처리됨] 여기 이 문장"
    assert result.session_id.startswith(DIAG_PREFIX)


async def test_round_trip_fails_when_nobody_answers() -> None:
    """블록이 안 떴을 때 '통과'로 보이면 이 화면은 아무 소용이 없다."""
    bus = InMemoryEventBus()
    result = await run_round_trip(bus, ROUND_TRIPS["FLT-MICRO"], tenant_id="t1", timeout_s=0.4)

    assert not result.ok
    assert "응답이 없다" in result.detail
    # 어디를 보라고 알려 준다 — "실패"만 던지면 결국 공급사 전화로 이어진다.
    assert "flt-micro" in result.detail


async def test_round_trip_ignores_other_sessions() -> None:
    """운영 트래픽이 흐르는 중에도 자기 응답만 봐야 한다.

    남의 응답을 자기 것으로 세면 죽은 블록이 통과로 나온다 — 이 화면이
    막아야 할 바로 그 상황이다.
    """
    bus = InMemoryEventBus()
    trip = ROUND_TRIPS["FLT-MICRO"]

    async def noise() -> None:
        for _ in range(5):
            await bus.publish(
                Topic.FILTER_CLEAN,
                FilterResult(
                    session_id="운영-세션",
                    tenant_id="t1",
                    channel=ChannelRole.CUSTOMER,
                    clean_text="남의 것",
                    is_final=True,
                ),
            )
            await asyncio.sleep(0.02)

    async def probe():  # type: ignore[no-untyped-def]
        task = asyncio.create_task(noise())
        try:
            return await run_round_trip(bus, trip, tenant_id="t1", timeout_s=0.5)
        finally:
            task.cancel()

    result = await probe()
    assert not result.ok, "남의 세션 응답을 자기 것으로 셌다"


async def test_round_trip_catches_a_fast_answer() -> None:
    """빠른 블록일수록 놓치기 쉽다 — 그룹을 만들기 전에 응답이 지나가기 때문이다."""
    bus = InMemoryEventBus()
    trip = ROUND_TRIPS["FLT-MICRO"]
    for _ in range(5):
        result = await _with_worker(
            _Echo(bus),
            run_round_trip(bus, trip, tenant_id="t1", timeout_s=2.0),
        )
        assert result.ok, "즉시 응답하는 블록을 놓쳤다"


async def test_round_trip_never_publishes_session_closed() -> None:
    """session.closed 하나면 요약 배치가 돈다 — 시험이 회의록을 만들면 안 된다."""
    inbound = {trip.inbound for trip in ROUND_TRIPS.values()}
    assert Topic.SESSION_CLOSED not in inbound


def test_probe_groups_do_not_collide_with_production() -> None:
    """운영 컨슈머 그룹에 끼어들면 시험이 운영 메시지를 가로챈다."""
    bus = InMemoryEventBus()
    from vai_core_adm.diagnose import _Collector

    groups = {
        _Collector(bus, Topic.FILTER_CLEAN, BaseEvent, diag_session()).group for _ in range(20)
    }
    assert len(groups) == 20, "시험 그룹이 서로 겹친다"
    assert all(group.startswith("diag:") for group in groups)


# ── 목록 ────────────────────────────────────────────────────────────────────


def test_every_event_only_block_has_a_round_trip() -> None:
    """HTTP가 없는 블록은 왕복 말고 시험할 길이 없다.

    빠지면 그 블록은 화면에 아예 나타나지 않고, 없는 것은 실패로도 보이지
    않는다 — 감사에서 반복해서 본 모양이다.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    # blocks/aud-vad/src/vai_aud_vad/main.py → AUD-VAD (패키지명이 아니라 블록 ID)
    event_only = {
        path.parents[2].name.upper()
        for path in repo.glob("blocks/*/src/*/main.py")
        if not list(path.parent.glob("app.py"))
    }
    assert event_only, "이벤트 전용 블록을 하나도 못 찾았다 — 경로 규칙이 낡았다"
    # 시험이 성립하지 않는 블록은 이유와 함께 뺀다.
    exempt = {
        "LLM-SUM": "session.closed 를 발행해야 하는데, 그것 하나로 요약 배치가 돈다",
    }
    covered = set(ROUND_TRIPS) | set(DIRECT_CALLS)
    missing = sorted(event_only - covered - set(exempt))
    assert not missing, f"이벤트 전용 블록인데 시험이 없다: {missing}"


def test_probe_list_matches_the_definitions() -> None:
    listed = {(item["block_id"], item["kind"]) for item in probes()}
    expected = {(b, "round_trip") for b in ROUND_TRIPS} | {(b, "direct") for b in DIRECT_CALLS}
    assert listed == expected


@pytest.mark.parametrize("block_id", sorted(set(ROUND_TRIPS) | set(DIRECT_CALLS)))
def test_every_probe_says_what_it_means(block_id: str) -> None:
    """결과 해석이 없으면 '응답 없음'이 고장인지 정상인지 모른다."""
    item = next(i for i in probes() if i["block_id"] == block_id)
    assert item["label"], f"{block_id}: 무엇을 시험하는지 이름이 없다"


# ── 출력 정리 ───────────────────────────────────────────────────────────────


def test_summarize_does_not_dump_audio_into_the_page() -> None:
    """PCM 이 그대로 오면 화면이 멎는다."""
    from vai_contracts.events import AudioSegment
    from vai_contracts.session import SessionProfile

    event = AudioSegment(
        session_id="s",
        tenant_id="t",
        channel=ChannelRole.CUSTOMER,
        sample_rate=16000,
        pcm=b"\x00" * 100_000,
        profile=SessionProfile.AICC,
        start_ms=0,
        duration_ms=1000,
    )
    out = _summarize(event)
    assert out["pcm"] == "(100000바이트)"
    assert all(len(str(value)) < 300 for value in out.values())


def test_summarize_drops_noise_fields() -> None:
    event = SttDelta(
        session_id="s", tenant_id="t", channel=ChannelRole.CUSTOMER, text=SAMPLE_TEXT, is_final=True
    )
    out = _summarize(event)
    assert "schema_version" not in out and "emitted_at" not in out
    assert out["text"] == SAMPLE_TEXT
