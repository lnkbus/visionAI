"""LLM-SUM 요약 테스트.

LLM은 JSON을 요구해도 코드펜스를 붙이고 설명을 덧붙이고 필드를 빠뜨린다.
그 현실을 그대로 케이스로 만든다.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from vai_common.bus import InMemoryEventBus
from vai_contracts.events import FilterResult, SessionClosed, SpeakerLabel
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import SummaryStatus
from vai_contracts.topics import Topic
from vai_llm_sum.store import InMemorySummaryStore
from vai_llm_sum.summarizer import parse_json_object, to_aicc_summary, to_meeting_summary
from vai_llm_sum.worker import SummaryWorker, TranscriptBuffer, TranscriptCollector

SESSION = "sess_sum_0001"
TENANT = "t1"

AICC_JSON = """{
  "category": "카드",
  "subcategory": "결제일 변경",
  "customer_request": "결제일 연기를 요청",
  "agent_response": "연체 전 최대 5일 가능 안내",
  "resolution": "신청 접수",
  "follow_up": "",
  "keywords": ["결제일", "연기"]
}"""


# ── 파싱 견고성 ──────────────────────────────────────────────────────────────


def test_plain_json_parses() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_code_fence_is_stripped() -> None:
    """지시해도 코드펜스를 붙이는 모델이 많다."""
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_surrounding_prose_is_tolerated() -> None:
    raw = '요청하신 요약입니다:\n{"a": 1}\n도움이 되었길 바랍니다.'

    assert parse_json_object(raw) == {"a": 1}


def test_garbage_yields_empty_dict_not_exception() -> None:
    """파싱 실패로 예외를 던지면 요약이 통째로 사라진다."""
    assert parse_json_object("모델이 그냥 문장으로 답했다") == {}


def test_aicc_summary_maps_all_fields() -> None:
    summary = to_aicc_summary(AICC_JSON)

    assert summary.category == "카드"
    assert summary.customer_request == "결제일 연기를 요청"
    assert summary.keywords == ["결제일", "연기"]


def test_missing_fields_become_empty_not_crash() -> None:
    summary = to_aicc_summary('{"category": "카드"}')

    assert summary.category == "카드"
    assert summary.resolution == ""


def test_meeting_action_items_accept_both_shapes() -> None:
    """모델이 객체 대신 문자열 배열을 주는 경우가 흔하다."""
    raw = """{"title": "주간회의", "action_items": [
        {"text": "보고서 작성", "owner": "김철", "due": "금요일"},
        "회의실 예약"
    ]}"""

    summary = to_meeting_summary(raw)

    assert len(summary.action_items) == 2
    assert summary.action_items[0].owner == "김철"
    assert summary.action_items[1].text == "회의실 예약"
    assert summary.action_items[1].owner == ""


def test_action_item_without_text_is_dropped() -> None:
    summary = to_meeting_summary('{"action_items": [{"owner": "김철"}]}')

    assert summary.action_items == []


# ── 녹취록 버퍼 ──────────────────────────────────────────────────────────────


def clean(text: str, *, channel: ChannelRole = ChannelRole.CUSTOMER, is_final: bool = True):
    return FilterResult(
        session_id=SESSION, tenant_id=TENANT, channel=channel, clean_text=text, is_final=is_final
    )


def test_buffer_labels_speakers() -> None:
    buffer = TranscriptBuffer()
    buffer.add(clean("결제일 연기 되나요?"))
    buffer.add(clean("최대 5일 가능합니다", channel=ChannelRole.AGENT))

    assert buffer.take(SESSION) == "고객: 결제일 연기 되나요?\n상담원: 최대 5일 가능합니다"


def test_buffer_ignores_interim_results() -> None:
    """중간 인식 결과를 넣으면 같은 문장이 두 번 들어간다."""
    buffer = TranscriptBuffer()
    buffer.add(clean("결제일 연", is_final=False))
    buffer.add(clean("결제일 연기 되나요?"))

    assert buffer.take(SESSION) == "고객: 결제일 연기 되나요?"


def test_buffer_is_drained_once() -> None:
    """두 번 요약되면 GPU 낭비이자 중복 산출물이다."""
    buffer = TranscriptBuffer()
    buffer.add(clean("한 마디"))

    assert buffer.take(SESSION)
    assert buffer.take(SESSION) == ""


def test_buffer_caps_runaway_sessions() -> None:
    buffer = TranscriptBuffer(max_utterances=3)
    for i in range(10):
        buffer.add(clean(f"발화 {i}"))

    assert len(buffer.take(SESSION).splitlines()) == 3


# ── 요약 워커 ────────────────────────────────────────────────────────────────


@pytest.fixture
def bus() -> InMemoryEventBus:
    bus = InMemoryEventBus()
    bus.register_group(Topic.FILTER_CLEAN, "transcript")
    bus.register_group(Topic.SESSION_CLOSED, "summary")
    return bus


async def run_session(
    bus: InMemoryEventBus,
    responder,
    *,
    profile: SessionProfile = SessionProfile.AICC,
    utterances: list[str] | None = None,
):
    store = InMemorySummaryStore()
    buffer = TranscriptBuffer()
    collector = TranscriptCollector(bus, buffer, group="transcript", consumer="c1")
    worker = SummaryWorker(bus, buffer, responder, store, group="summary", consumer="w1")
    tasks = [asyncio.create_task(collector.run()), asyncio.create_task(worker.run())]
    try:
        for text in utterances if utterances is not None else ["결제일 연기 되나요?"]:
            await bus.publish(Topic.FILTER_CLEAN, clean(text))
        await asyncio.sleep(0.05)
        await bus.publish(
            Topic.SESSION_CLOSED,
            SessionClosed(session_id=SESSION, tenant_id=TENANT, profile=profile),
        )
        for _ in range(50):
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


async def test_session_close_produces_a_summary(bus: InMemoryEventBus) -> None:
    async def responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        return AICC_JSON

    summary = await run_session(bus, responder)

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert summary.aicc is not None
    assert summary.aicc.category == "카드"
    assert summary.prompt_version == "aicc-v1", (
        "감사 시 어떤 프롬프트로 만들었는지 답할 수 있어야 한다"
    )


async def test_meeting_profile_uses_the_meeting_prompt(bus: InMemoryEventBus) -> None:
    captured: dict[str, str] = {}

    async def responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        captured["system"] = system
        return '{"title": "주간회의", "agenda": ["예산"]}'

    summary = await run_session(bus, responder, profile=SessionProfile.MEETING)

    assert summary is not None and summary.meeting is not None
    assert summary.meeting.title == "주간회의"
    assert summary.prompt_version == "meeting-v1"
    assert "회의록" in captured["system"]


async def test_empty_transcript_skips_the_model(bus: InMemoryEventBus) -> None:
    """발화 없는 세션(오접속)에 GPU를 쓰지 않는다."""
    called = False

    async def responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        nonlocal called
        called = True
        return AICC_JSON

    summary = await run_session(bus, responder, utterances=[])

    assert summary is not None
    assert summary.status is SummaryStatus.READY
    assert not called


async def test_model_failure_is_recorded_not_swallowed(bus: InMemoryEventBus) -> None:
    async def responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        raise RuntimeError("vLLM 응답 없음")

    summary = await run_session(bus, responder)

    assert summary is not None
    assert summary.status is SummaryStatus.FAILED
    assert summary.error is not None and "vLLM" in summary.error


async def test_empty_model_output_is_marked_failed(bus: InMemoryEventBus) -> None:
    """빈 요약을 READY로 올리면 상담 이력에 빈 레코드가 쌓이고 아무도 모른다."""

    async def responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        return "모델이 요약을 거부했습니다"

    summary = await run_session(bus, responder)

    assert summary is not None
    assert summary.status is SummaryStatus.FAILED


async def test_transcript_is_masked_text_only(bus: InMemoryEventBus) -> None:
    """원문을 LLM에 넣으면 개인정보가 프롬프트를 타고 나간다(docs/05 §2.1)."""
    captured: dict[str, str] = {}

    async def responder(*, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        captured["prompt"] = prompt
        return AICC_JSON

    await run_session(bus, responder, utterances=["제 번호는 [RRN_MASKED] 입니다"])

    assert "[RRN_MASKED]" in captured["prompt"]
    assert "900101" not in captured["prompt"]


# ── 화자 라벨 결합 ───────────────────────────────────────────────────────────


def labeled(start_ms: int, speaker_id: str) -> SpeakerLabel:
    return SpeakerLabel(
        session_id=SESSION,
        tenant_id=TENANT,
        channel=ChannelRole.PARTICIPANT,
        speaker_id=speaker_id,
        start_ms=start_ms,
        duration_ms=1500,
    )


def meeting_line(text: str, start_ms: int) -> FilterResult:
    return FilterResult(
        session_id=SESSION,
        tenant_id=TENANT,
        channel=ChannelRole.PARTICIPANT,
        clean_text=text,
        start_ms=start_ms,
    )


def test_late_speaker_label_is_applied_to_the_transcript() -> None:
    """회의록에서 화자분리의 값어치는 여기다 — 라벨이 늦게 와도 붙어야 한다."""
    buffer = TranscriptBuffer()
    buffer.add(meeting_line("예산 집행부터 보겠습니다", 1000))
    buffer.add(meeting_line("이월 승인이 필요합니다", 4000))

    assert buffer.label(labeled(1000, "speaker_1"))
    assert buffer.label(labeled(4000, "speaker_2"))

    assert buffer.take(SESSION) == (
        "speaker_1: 예산 집행부터 보겠습니다\nspeaker_2: 이월 승인이 필요합니다"
    )


def test_label_without_a_matching_utterance_is_dropped() -> None:
    """자막이 없는 구간에 라벨을 붙이면 회의록에 유령 발화가 생긴다."""
    buffer = TranscriptBuffer()
    buffer.add(meeting_line("한 마디", 1000))

    assert not buffer.label(labeled(9999, "speaker_3"))


def test_unlabeled_utterances_fall_back_to_the_channel() -> None:
    """SPK-DIA 미배포·AICC 프로파일에서도 요약은 만들어져야 한다."""
    buffer = TranscriptBuffer()
    buffer.add(clean("결제일 연기 되나요?"))

    assert buffer.take(SESSION) == "고객: 결제일 연기 되나요?"
