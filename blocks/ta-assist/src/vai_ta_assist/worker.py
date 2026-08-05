"""TA-ASSIST 워커 — ``filter.clean`` → ``assist.popup``.

사양서 §3 모듈 3의 전 단계를 조립한다:
질의 추출(SLM <200ms) → 하이브리드 검색(<100ms) → 리랭킹(<80ms) → UI 푸시(<50ms).

전체 예산 1초를 이 워커가 책임진다. 초과하면 경고 로그가 남고, 그 로그가
docs/06 §5의 지연 회귀 감시 입력이 된다.
"""

from __future__ import annotations

import asyncio
import logging
import time

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import AssistPopup, FilterResult, KnowledgeHit
from vai_contracts.retrieval import ScoredChunk, SearchRequest
from vai_contracts.session import ChannelRole
from vai_contracts.topics import Topic
from vai_contracts.ws import AgentAssistUpdate
from vai_ta_assist.answer import AnswerComposer
from vai_ta_assist.extractor import ConversationWindow, QueryExtractor, should_search
from vai_ta_assist.ports import SearchPort

log = logging.getLogger(__name__)
BLOCK_ID = "TA-ASSIST"

SNIPPET_CHARS = 180
"""팝업에 보여줄 근거 길이. 상담 중 훑어볼 수 있는 분량을 넘기면 안 읽힌다."""

ANSWER_BUDGET_MS = 2000.0
"""추천 답변 생성에 허용하는 시간.

**근거 팝업의 1초 예산과는 별개다.** 근거는 먼저 띄우고 답변은 뒤따라 채운다 —
답변을 기다리느라 근거까지 늦어지면 있는 정보도 못 쓰게 된다. 이 시간을 넘기면
답변 없이 끝낸다. 상담원은 이미 다음 말을 하고 있다."""


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
        composer: AnswerComposer | None = None,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._extractor = extractor
        self._search = search
        self._composer = composer or AnswerComposer()
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
        # 근거를 먼저 띄운다. 추천 답변은 뒤따라 채운다 — 답변을 기다리느라
        # 근거까지 늦어지면 1초 예산 안에 아무것도 못 보여주게 된다.
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

        if self._composer.enabled:
            await self._attach_answer(event.session_id, query, response.hits, popup)

        # 버스에는 최종 형태 한 번만 싣는다. 채택률 분석이 같은 팝업을 두 건으로
        # 세면 지표가 어긋난다.
        await self.bus.publish(Topic.ASSIST_POPUP, popup)

    async def _attach_answer(
        self,
        session_id: str,
        query: str,
        hits: list[ScoredChunk],
        popup: AssistPopup,
    ) -> None:
        """검증을 통과한 추천 답변만 팝업에 채워 다시 보낸다."""
        started = time.perf_counter()
        try:
            verdict = await asyncio.wait_for(
                self._composer.compose(query, hits), ANSWER_BUDGET_MS / 1000
            )
        except TimeoutError:
            log.warning(
                "추천 답변 생성 시간 초과 — 근거만 유지",
                extra={"session_id": session_id, "budget_ms": ANSWER_BUDGET_MS},
            )
            return

        if not verdict.accepted:
            return

        # 답변은 자신이 인용한 근거 카드에 붙인다. 다른 카드에 붙으면 상담원이
        # 엉뚱한 조항을 근거로 읽게 된다.
        target = verdict.citations[0] - 1
        if not 0 <= target < len(popup.hits):
            return
        popup.hits[target].recommended_answer = verdict.answer

        await self.bus.publish_ui(session_id, AgentAssistUpdate.from_popup(popup))
        log.info(
            "추천 답변 첨부",
            extra={
                "session_id": session_id,
                "citations": verdict.citations,
                "compose_ms": int((time.perf_counter() - started) * 1000),
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
        # 비워 둔 채로 먼저 보낸다. 검증을 통과한 추천 답변이 있으면
        # :meth:`AssistWorker._attach_answer`가 채워 다시 보낸다. 검증을 통과하지
        # 못하면 끝까지 비어 있고, 화면에는 근거 원문만 남는다 — 없는 답을
        # 지어내느니 원문을 보여주는 편이 낫다.
        recommended_answer="",
    )
