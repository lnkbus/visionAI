"""TA-ASSIST 워커 — ``filter.clean`` → ``assist.popup``.

사양서 §3 모듈 3의 전 단계를 조립한다:
질의 추출(SLM <200ms) → 하이브리드 검색(<100ms) → 리랭킹(<80ms) → UI 푸시(<50ms).

전체 예산 1초를 이 워커가 책임진다. 초과하면 경고 로그가 남고, 그 로그가
docs/06 §5의 지연 회귀 감시 입력이 된다.
"""

from __future__ import annotations

import logging
import time

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import AssistPopup, FilterResult, KnowledgeHit
from vai_contracts.retrieval import ScoredChunk, SearchRequest
from vai_contracts.session import ChannelRole
from vai_contracts.topics import Topic
from vai_contracts.ws import AgentAssistUpdate
from vai_ta_assist.extractor import ConversationWindow, QueryExtractor, should_search
from vai_ta_assist.ports import SearchPort

log = logging.getLogger(__name__)
BLOCK_ID = "TA-ASSIST"

SNIPPET_CHARS = 180
"""팝업에 보여줄 근거 길이. 상담 중 훑어볼 수 있는 분량을 넘기면 안 읽힌다."""


class AssistWorker(BlockWorker[FilterResult]):
    block_id = BLOCK_ID
    source_topic = Topic.FILTER_CLEAN
    source_model = FilterResult
    latency_budget_ms = 1000.0
    """사양서 §1 Sub-Second Pipeline. STT 수신부터 팝업까지의 총 예산."""

    def __init__(
        self,
        bus: EventBus,
        extractor: QueryExtractor,
        search: SearchPort,
        *,
        group: str,
        consumer: str,
        kb_id: str = "default",
        top_k: int = 3,
        min_score: float = 0.0,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._extractor = extractor
        self._search = search
        self._kb_id = kb_id
        self._top_k = top_k
        self._min_score = min_score
        self._windows: dict[str, ConversationWindow] = {}

    def _window(self, session_id: str) -> ConversationWindow:
        return self._windows.setdefault(session_id, ConversationWindow())

    async def handle(self, event: FilterResult) -> None:
        window = self._window(event.session_id)

        # 중간 인식 결과는 맥락에 넣지 않는다. 같은 문장이 확정될 때 다시 오므로
        # 넣으면 창이 미완성 조각으로 채워져 질의 추출이 나빠진다.
        if not event.is_final:
            return
        window.add(event.channel, event.clean_text)

        # 검색은 고객 발화에만 건다. 상담원 발화는 이미 답변이라 팝업이 필요 없다.
        if event.channel is not ChannelRole.CUSTOMER or not should_search(event.clean_text):
            return

        started = time.perf_counter()
        query = await self._extractor.extract(window)
        if not query:
            return

        response = await self._search(
            SearchRequest(
                tenant_id=event.tenant_id,
                kb_id=self._kb_id,
                query=query,
                top_k=self._top_k,
                min_score=self._min_score,
            )
        )
        if not response.hits:
            return

        popup = AssistPopup(
            session_id=event.session_id,
            tenant_id=event.tenant_id,
            seq=event.seq,
            query=query,
            hits=[_to_hit(hit) for hit in response.hits],
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        await self.bus.publish(Topic.ASSIST_POPUP, popup)
        await self.bus.publish_ui(event.session_id, AgentAssistUpdate.from_popup(popup))

        log.info(
            "지식 팝업 전송",
            extra={
                "session_id": event.session_id,
                "query": query,
                "hit_count": len(popup.hits),
                "latency_ms": popup.latency_ms,
                "search_ms": response.latency_ms,
            },
        )

    def release_session(self, session_id: str) -> None:
        self._windows.pop(session_id, None)


def _to_hit(scored: ScoredChunk) -> KnowledgeHit:
    """검색 결과를 사양서 §4의 ``knowledge_popup`` 형태로 바꾼다."""
    text = scored.chunk.text.strip()
    snippet = text[:SNIPPET_CHARS] + ("..." if len(text) > SNIPPET_CHARS else "")
    return KnowledgeHit(
        doc_id=scored.chunk.doc_id,
        title=scored.chunk.title,
        score=round(scored.score, 4),
        snippet=snippet,
        # 추천 답변 생성(sLLM)은 Wave 4에서 붙인다. 지금은 근거 원문을 그대로
        # 보여준다 — 없는 답을 지어내느니 원문을 보여주는 편이 낫다.
        recommended_answer="",
    )
