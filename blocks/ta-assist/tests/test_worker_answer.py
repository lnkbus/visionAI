"""워커의 팝업 순서와 폴백 검증.

두 가지가 지켜져야 한다:

1. **근거가 먼저 뜬다.** 답변 생성을 기다리다 근거까지 늦어지면 1초 예산 안에
   화면에 아무것도 없다.
2. **검증에 걸린 답변은 화면에 도달하지 않는다.** 이 경로가 새면 앞의 모든
   검증이 장식이 된다.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from vai_common.bus import InMemoryEventBus
from vai_contracts.events import AssistPopup, BaseEvent, FilterResult
from vai_contracts.retrieval import Chunk, ScoredChunk, SearchRequest, SearchResponse
from vai_contracts.session import ChannelRole
from vai_contracts.topics import Topic
from vai_ta_assist.answer import AnswerComposer
from vai_ta_assist.extractor import QueryExtractor
from vai_ta_assist.worker import AssistWorker

PASSAGE = "재발급 수수료는 3,000원이며 발급까지 영업일 기준 3일이 소요된다."


class RecordingBus(InMemoryEventBus):
    """UI로 나간 순서를 그대로 기록한다 — 여기서 재는 것이 곧 상담원이 본 순서다."""

    def __init__(self) -> None:
        super().__init__()
        self.ui: list[list[str]] = []
        self.events: list[AssistPopup] = []

    async def publish_ui(self, session_id: str, payload: BaseModel) -> None:
        hits = getattr(payload, "knowledge_popup", [])
        self.ui.append([hit.recommended_answer for hit in hits])
        await super().publish_ui(session_id, payload)

    async def publish(self, topic: Topic, event: BaseEvent) -> None:
        if isinstance(event, AssistPopup):
            self.events.append(event.model_copy(deep=True))
        await super().publish(topic, event)


async def _search(request: SearchRequest) -> SearchResponse:
    return SearchResponse(
        hits=[
            ScoredChunk(
                chunk=Chunk(
                    chunk_id="c1",
                    doc_id="제3조",
                    tenant_id=request.tenant_id,
                    kb_id=request.kb_id,
                    title="제3조 (카드의 발급과 재발급)",
                    text=PASSAGE,
                ),
                score=1.0,
            )
        ]
    )


def _event() -> FilterResult:
    return FilterResult(
        session_id="s1",
        tenant_id="t1",
        seq=1,
        channel=ChannelRole.CUSTOMER,
        clean_text="재발급 수수료가 얼마인가요",
        is_final=True,
    )


async def _run(composer: AnswerComposer) -> RecordingBus:
    bus = RecordingBus()
    worker = AssistWorker(
        bus,
        QueryExtractor(),
        _search,
        group="assist",
        consumer="assist-1",
        composer=composer,
    )
    await worker.handle(_event())
    return bus


async def test_evidence_is_pushed_before_the_answer() -> None:
    async def honest(prompt: str, system: str = "") -> str:
        return "재발급 수수료는 3,000원입니다. [1]"

    bus = await _run(AnswerComposer(honest))

    assert len(bus.ui) == 2, "근거 먼저, 답변은 뒤따라 — 두 번 나가야 한다"
    assert bus.ui[0] == [""], "첫 푸시는 근거만"
    assert "3,000원" in bus.ui[1][0]


async def test_rejected_answer_never_reaches_the_screen() -> None:
    async def liar(prompt: str, system: str = "") -> str:
        return "재발급 수수료는 9,900원입니다. [1]"

    bus = await _run(AnswerComposer(liar))

    assert len(bus.ui) == 1
    assert bus.ui[0] == [""]


async def test_slow_generation_does_not_hold_the_popup() -> None:
    """생성이 늦으면 답변만 포기한다. 근거는 이미 화면에 있다."""

    async def slow(prompt: str, system: str = "") -> str:
        await asyncio.sleep(5)
        return "재발급 수수료는 3,000원입니다. [1]"

    bus = RecordingBus()
    worker = AssistWorker(
        bus,
        QueryExtractor(),
        _search,
        group="assist",
        consumer="assist-1",
        composer=AnswerComposer(slow),
    )
    # 예산을 줄여 실제 대기 없이 초과 상황을 만든다.
    from vai_ta_assist import worker as worker_module

    original = worker_module.ANSWER_BUDGET_MS
    worker_module.ANSWER_BUDGET_MS = 20.0
    try:
        await worker.handle(_event())
    finally:
        worker_module.ANSWER_BUDGET_MS = original

    assert len(bus.ui) == 1
    assert bus.ui[0] == [""]


async def test_bus_event_carries_the_final_shape_once() -> None:
    """채택률 분석이 같은 팝업을 두 건으로 세면 지표가 어긋난다."""

    async def honest(prompt: str, system: str = "") -> str:
        return "재발급 수수료는 3,000원입니다. [1]"

    bus = await _run(AnswerComposer(honest))
    assert len(bus.events) == 1
    assert "3,000원" in bus.events[0].hits[0].recommended_answer


async def test_without_a_composer_nothing_changes() -> None:
    bus = await _run(AnswerComposer())

    assert len(bus.ui) == 1
    assert bus.events[0].hits[0].recommended_answer == ""
