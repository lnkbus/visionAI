"""Wave 3 통합 테스트 — 사양서의 1초 미만 Agent Assist 경로.

``audio.in`` 한 건이 VAD → STT → 필터 → 질의추출 → 검색 → 리랭킹을 거쳐
상담원 화면의 지식 팝업으로 나오는 전 구간을 확인한다. GPU도 Redis도 쓰지 않는다.

여기서 검증하는 것은 **블록 조립**이다. 각 블록의 내부 품질은 블록별 테스트가
따로 본다.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from tests.conftest import SAMPLE_RATE, pcm_silence, pcm_tone
from vai_aud_vad.adapters.energy import EnergyVadAdapter
from vai_aud_vad.worker import VadWorker
from vai_common.bus import InMemoryEventBus
from vai_contracts.events import AssistPopup, AudioChunk, FilterResult
from vai_contracts.retrieval import Chunk, SearchRequest
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.topics import Topic
from vai_flt_micro.filter import MicroComplianceFilter, load_rules
from vai_flt_micro.worker import FilterWorker
from vai_retrieval.embedding import HashingEmbedder
from vai_retrieval.hybrid import HybridSearchEngine
from vai_retrieval.rerank import LexicalOverlapReranker
from vai_retrieval.store import MemoryVectorStore
from vai_stt_core.adapters.fake import FakeSTTAdapter
from vai_stt_core.worker import SttWorker
from vai_ta_assist.extractor import QueryExtractor
from vai_ta_assist.worker import AssistWorker

SESSION = "sess_assist_0001"
TENANT = "tenant_assist"
KB = "default"

# STT가 내보낼 대본. 사양서 §4의 예시 대화를 그대로 쓴다.
SCRIPT = [
    "이번 달 카드 대금 결제일 연기가 가능한가요?",
    "제 주민등록번호는 900101-1234567 입니다.",
]

KNOWLEDGE = [
    (
        "doc_card_policy_012",
        "신용카드 결제일 변경 및 연기 규정",
        "단기 결제 연기는 연체 발생 전 고객센터 및 앱을 통해 최대 5일까지 신청 가능합니다.",
    ),
    (
        "doc_card_lost_003",
        "카드 분실 신고 절차",
        "카드를 분실한 경우 즉시 고객센터로 신고해야 하며 신고 시점부터 보상이 적용됩니다.",
    ),
]

RULES = [
    {
        "rule_id": "PII_SOLICITATION",
        "pattern": r"주민(등록)?번호.*(불러|말씀|알려)",
        "severity": "warning",
        "message": "음성으로 주민번호를 받지 않는다",
    }
]


async def build_search() -> HybridSearchEngine:
    embedder = HashingEmbedder()
    await embedder.initialize("", {})
    store = MemoryVectorStore()
    await store.initialize({"multiprocess_warning": False})
    reranker = LexicalOverlapReranker()
    await reranker.initialize("", {})
    engine = HybridSearchEngine(embedder, store, reranker)
    await engine.index_chunks(
        [
            Chunk(
                chunk_id=f"{doc_id}:0",
                doc_id=doc_id,
                tenant_id=TENANT,
                kb_id=KB,
                title=title,
                text=text,
            )
            for doc_id, title, text in KNOWLEDGE
        ]
    )
    return engine


@pytest.fixture
async def pipeline(bus: InMemoryEventBus):
    """VAD → STT → FLT → TA-ASSIST 네 워커를 조립한다."""
    vad = EnergyVadAdapter()
    await vad.initialize({})
    stt = FakeSTTAdapter(SCRIPT)
    await stt.initialize("", {})
    engine = await build_search()

    workers = [
        VadWorker(bus, vad, group="vad", consumer="vad-1"),
        # 필터가 붙었으므로 STT의 UI 직송은 끈다. 마스킹 전 텍스트가 화면에
        # 스치면 안 된다 — 이 설정이 그 보장의 전부다.
        SttWorker(bus, stt, group="stt", consumer="stt-1", publish_ui=False),
        FilterWorker(bus, MicroComplianceFilter(load_rules(RULES)), group="flt", consumer="flt-1"),
        AssistWorker(
            bus,
            QueryExtractor(),
            engine.search,
            group="assist",
            consumer="assist-1",
            kb_id=KB,
        ),
    ]

    for topic, group in [
        (Topic.AUDIO_IN, "vad"),
        (Topic.AUDIO_SEGMENT, "stt"),
        (Topic.STT_DELTA, "flt"),
        (Topic.FILTER_CLEAN, "assist"),
        (Topic.FILTER_CLEAN, "observer"),
        (Topic.ASSIST_POPUP, "observer"),
    ]:
        bus.register_group(topic, group)

    tasks = [asyncio.create_task(worker.run()) for worker in workers]
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


async def speak(bus: InMemoryEventBus, seq: int) -> None:
    """발화 한 마디 + 뒤따르는 무음(발화 종료 신호)."""
    await bus.publish(Topic.AUDIO_IN, audio(pcm_tone(600), seq))
    await bus.publish(Topic.AUDIO_IN, audio(pcm_silence(700), seq + 1))


async def await_event(bus: InMemoryEventBus, topic: Topic, model: type, wait_s: float = 5.0):
    async def run():
        async for delivery in bus.consume(topic, model, group="observer", consumer="obs"):
            await delivery.ack()
            return delivery.event
        return None

    with suppress(TimeoutError):
        return await asyncio.wait_for(run(), timeout=wait_s)
    return None


async def test_customer_question_produces_a_knowledge_popup(
    pipeline: InMemoryEventBus,
) -> None:
    """Wave 3이 증명해야 하는 경로: 질문 한 마디 → 근거 팝업."""
    watcher = asyncio.create_task(await_event(pipeline, Topic.ASSIST_POPUP, AssistPopup))
    await asyncio.sleep(0)

    await speak(pipeline, 1)
    popup = await watcher

    assert popup is not None, "지식 팝업이 나오지 않았다"
    assert popup.session_id == SESSION
    assert popup.tenant_id == TENANT
    assert popup.hits, "근거 없는 팝업은 의미가 없다"
    assert popup.hits[0].doc_id == "doc_card_policy_012"
    assert "결제일" in popup.hits[0].title
    assert popup.hits[0].snippet


async def test_popup_stays_within_the_sub_second_budget(
    pipeline: InMemoryEventBus,
) -> None:
    """사양서 §1 Sub-Second Pipeline. fake 어댑터 기준의 하한 확인이다 —
    실제 SLM·임베딩 모델 지연은 부하 테스트로 따로 잰다(docs/06 §5)."""
    watcher = asyncio.create_task(await_event(pipeline, Topic.ASSIST_POPUP, AssistPopup))
    await asyncio.sleep(0)

    await speak(pipeline, 1)
    popup = await watcher

    assert popup is not None
    assert popup.latency_ms < 1000, f"1초 예산 초과: {popup.latency_ms}ms"


async def test_pii_is_masked_before_it_can_reach_the_screen(
    pipeline: InMemoryEventBus,
) -> None:
    """주민번호가 포함된 두 번째 발화가 마스킹된 채로만 흘러야 한다."""
    watcher = asyncio.create_task(await_event(pipeline, Topic.FILTER_CLEAN, FilterResult))
    await asyncio.sleep(0)

    await speak(pipeline, 1)
    first = await watcher

    assert first is not None
    assert first.clean_text == SCRIPT[0], "첫 발화는 마스킹 대상이 아니다"

    watcher = asyncio.create_task(await_event(pipeline, Topic.FILTER_CLEAN, FilterResult))
    await asyncio.sleep(0)
    await speak(pipeline, 3)
    second = await watcher

    assert second is not None
    assert second.pii_masked
    assert "900101" not in second.clean_text
    assert "[RRN_MASKED]" in second.clean_text


async def test_ui_receives_masked_captions_and_popups(pipeline: InMemoryEventBus) -> None:
    """화면으로 나가는 것은 필터를 통과한 텍스트뿐이어야 한다."""
    received: list[dict] = []

    async def watch() -> None:
        async for payload in pipeline.subscribe_ui(SESSION):
            received.append(payload)

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)

    await speak(pipeline, 1)
    await asyncio.sleep(0.3)
    watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher

    captions = [m for m in received if m.get("stt_result")]
    popups = [m for m in received if m.get("knowledge_popup")]

    assert captions, "자막이 화면으로 가지 않았다"
    assert popups, "팝업이 화면으로 가지 않았다"
    # 필터가 붙은 뒤에도 STT가 직접 UI로 쏘고 있으면 마스킹 전 텍스트가 새어 나간다.
    assert all(caption["compliance"] is not None for caption in captions), (
        "화면 자막은 전부 필터를 거친 것이어야 한다"
    )


async def test_search_scopes_to_the_calling_tenant() -> None:
    """다른 테넌트의 약관이 팝업으로 뜨면 그 자체가 사고다."""
    engine = await build_search()

    response = await engine.search(
        SearchRequest(tenant_id="someone-else", kb_id=KB, query="결제일 연기")
    )

    assert response.hits == []
