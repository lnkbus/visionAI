"""LLM-SUM 워커 — ``session.closed`` → ``summary.done``.

배치 경로다. 실시간 예산이 없는 대신 GPU를 오래 물므로, 실시간 블록과
프로세스를 분리해 상담 중 팝업을 밀어내지 않게 한다.

녹취록은 **마스킹본**(``filter.clean``)만 쓴다. 원문을 LLM에 넣으면
개인정보가 프롬프트를 타고 나간다(docs/05 §2.1).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import FilterResult, SessionClosed
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import Summary, SummaryStatus
from vai_contracts.topics import Topic
from vai_llm_sum import prompts
from vai_llm_sum.ports import CompletePort, SummaryStore
from vai_llm_sum.summarizer import is_empty, to_aicc_summary, to_meeting_summary

log = logging.getLogger(__name__)
BLOCK_ID = "LLM-SUM"

_LABELS = {ChannelRole.CUSTOMER: "고객", ChannelRole.AGENT: "상담원"}


class TranscriptBuffer:
    """세션별 마스킹 발화 누적.

    ``filter.clean``을 지나가며 모아 둔다. 종료 시점에 다시 조회하는 대신
    스트림에서 모으는 이유: 상담 이력 DB(Wave 6)가 아직 없고, 있더라도
    요약을 위해 전체를 다시 읽는 것은 낭비다.
    """

    def __init__(self, max_utterances: int = 2000) -> None:
        self._lines: dict[str, list[str]] = defaultdict(list)
        self._max = max_utterances

    def add(self, event: FilterResult) -> None:
        if not event.is_final or not event.clean_text.strip():
            return
        lines = self._lines[event.session_id]
        if len(lines) >= self._max:
            # 폭주하는 세션이 메모리를 잠식하지 않게 한다.
            return
        speaker = event.speaker_id or _LABELS.get(event.channel, "참여자")
        lines.append(f"{speaker}: {event.clean_text.strip()}")

    def take(self, session_id: str) -> str:
        return "\n".join(self._lines.pop(session_id, []))

    def drop(self, session_id: str) -> None:
        self._lines.pop(session_id, None)


class TranscriptCollector(BlockWorker[FilterResult]):
    """``filter.clean``을 구독해 녹취록을 모은다."""

    block_id = BLOCK_ID
    source_topic = Topic.FILTER_CLEAN
    source_model = FilterResult

    def __init__(
        self, bus: EventBus, buffer: TranscriptBuffer, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._buffer = buffer

    async def handle(self, event: FilterResult) -> None:
        self._buffer.add(event)


class SummaryWorker(BlockWorker[SessionClosed]):
    """``session.closed``를 받아 요약을 생성한다."""

    block_id = BLOCK_ID
    source_topic = Topic.SESSION_CLOSED
    source_model = SessionClosed
    latency_budget_ms = 30_000.0
    """배치 경로라 넉넉하다. 이걸 넘으면 모델이 과하거나 녹취록이 안 잘렸다는 뜻."""

    def __init__(
        self,
        bus: EventBus,
        buffer: TranscriptBuffer,
        complete: CompletePort,
        store: SummaryStore,
        *,
        group: str,
        consumer: str,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._buffer = buffer
        self._complete = complete
        self._store = store

    async def handle(self, event: SessionClosed) -> None:
        transcript = self._buffer.take(event.session_id)
        summary = Summary(
            session_id=event.session_id,
            tenant_id=event.tenant_id,
            profile=event.profile,
            transcript_chars=len(transcript),
        )

        if not transcript.strip():
            # 발화 없는 세션(오접속·즉시 끊김)에 GPU를 쓰지 않는다.
            summary.status = SummaryStatus.READY
            await self._store.put(summary)
            return

        summary.status = SummaryStatus.RUNNING
        await self._store.put(summary)

        spec = prompts.MEETING if event.profile is SessionProfile.MEETING else prompts.AICC
        summary.prompt_version = spec.version
        started = time.perf_counter()

        try:
            raw = await self._complete(
                prompt=prompts.build_user_prompt(transcript),
                system=spec.system,
                tenant_id=event.tenant_id,
                max_tokens=800,
            )
        except Exception as exc:
            summary.status = SummaryStatus.FAILED
            summary.error = str(exc)
            log.exception("요약 생성 실패", extra={"session_id": event.session_id})
            await self._store.put(summary)
            return

        summary.latency_ms = int((time.perf_counter() - started) * 1000)

        if event.profile is SessionProfile.MEETING:
            summary.meeting = to_meeting_summary(raw)
            empty = is_empty(summary.meeting)
        else:
            summary.aicc = to_aicc_summary(raw)
            empty = is_empty(summary.aicc)

        if empty:
            # 파싱은 됐지만 내용이 비었다. 빈 요약을 READY로 올리면 상담 이력에
            # 빈 레코드가 쌓이고 아무도 눈치채지 못한다.
            summary.status = SummaryStatus.FAILED
            summary.error = "모델 응답에서 요약 내용을 얻지 못했다"
            log.warning("빈 요약", extra={"session_id": event.session_id})
        else:
            summary.status = SummaryStatus.READY

        await self._store.put(summary)
        await self.bus.publish_ui(event.session_id, summary)
        log.info(
            "요약 완료",
            extra={
                "session_id": event.session_id,
                "profile": event.profile.value,
                "status": summary.status.value,
                "latency_ms": summary.latency_ms,
            },
        )
