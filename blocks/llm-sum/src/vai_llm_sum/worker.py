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
from dataclasses import dataclass

from vai_common.bus import EventBus
from vai_common.resource import Yield
from vai_common.worker import BlockWorker
from vai_contracts.events import FilterResult, SessionClosed, SpeakerLabel
from vai_contracts.session import ChannelRole, SessionProfile
from vai_contracts.summary import Summary, SummaryDone, SummaryStatus
from vai_contracts.topics import Topic
from vai_llm_sum import prompts
from vai_llm_sum.ports import CompletePort, SummaryStore
from vai_llm_sum.summarizer import is_empty, to_aicc_summary, to_meeting_summary

log = logging.getLogger(__name__)
BLOCK_ID = "LLM-SUM"

_LABELS = {ChannelRole.CUSTOMER: "고객", ChannelRole.AGENT: "상담원"}


@dataclass
class _Utterance:
    start_ms: int
    speaker: str
    text: str


class TranscriptBuffer:
    """세션별 마스킹 발화 누적.

    ``filter.clean``을 지나가며 모아 둔다. 종료 시점에 다시 조회하는 대신
    스트림에서 모으는 이유: 상담 이력 DB(Wave 6)가 아직 없고, 있더라도
    요약을 위해 전체를 다시 읽는 것은 낭비다.

    회의 프로파일에서는 화자 라벨(SPK-DIA)이 자막보다 **늦게** 도착한다.
    발화를 ``start_ms``로 색인해 두고, 라벨이 오면 그 자리의 화자를 바꾼다.
    회의록은 종료 후 만들어지므로 그때까지만 맞으면 된다.
    """

    def __init__(self, max_utterances: int = 2000) -> None:
        self._lines: dict[str, list[_Utterance]] = defaultdict(list)
        self._by_start: dict[str, dict[int, _Utterance]] = defaultdict(dict)
        self._max = max_utterances

    def add(self, event: FilterResult) -> None:
        if not event.is_final or not event.clean_text.strip():
            return
        lines = self._lines[event.session_id]
        if len(lines) >= self._max:
            # 폭주하는 세션이 메모리를 잠식하지 않게 한다.
            return
        utterance = _Utterance(
            start_ms=event.start_ms,
            speaker=event.speaker_id or _LABELS.get(event.channel, "참여자"),
            text=event.clean_text.strip(),
        )
        lines.append(utterance)
        # 같은 start_ms가 겹치면 나중 것이 이긴다 — 재처리된 구간의 최신 인식이다.
        self._by_start[event.session_id][event.start_ms] = utterance

    def label(self, event: SpeakerLabel) -> bool:
        """뒤늦게 도착한 화자 라벨을 해당 발화에 붙인다."""
        utterance = self._by_start[event.session_id].get(event.start_ms)
        if utterance is None:
            # 자막이 아직 안 왔거나 필터에서 걸러진 구간. 회의록에 실릴 발화가
            # 없다는 뜻이므로 라벨도 버린다.
            return False
        utterance.speaker = event.speaker_id
        return True

    def take(self, session_id: str) -> str:
        lines = self._lines.pop(session_id, [])
        self._by_start.pop(session_id, None)
        return "\n".join(f"{line.speaker}: {line.text}" for line in lines)

    def drop(self, session_id: str) -> None:
        self._lines.pop(session_id, None)
        self._by_start.pop(session_id, None)


class SpeakerLabelCollector(BlockWorker[SpeakerLabel]):
    """``speaker.label``을 구독해 녹취록의 화자를 채운다."""

    block_id = BLOCK_ID
    source_topic = Topic.SPEAKER_LABEL
    source_model = SpeakerLabel

    def __init__(
        self, bus: EventBus, buffer: TranscriptBuffer, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._buffer = buffer

    async def handle(self, event: SpeakerLabel) -> None:
        self._buffer.label(event)


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
        yield_to_stt: Yield | None = None,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._buffer = buffer
        self._complete = complete
        self._store = store
        self._yield_to_stt = yield_to_stt

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
            await self._announce(summary)
            return

        # 실시간 경로에 양보한다. 기다리는 동안 상태를 저장해 **화면에 보이게**
        # 한다 — 아무 표시가 없으면 사용자는 요약이 실패했다고 생각한다.
        if self._yield_to_stt is not None and self._yield_to_stt.enabled:
            summary.status = SummaryStatus.WAITING
            await self._store.put(summary)
            waited = await self._yield_to_stt.wait_for_turn()
            summary.waited_for_stt_ms = int(waited * 1000)
            summary.yield_gave_up = self._yield_to_stt.gave_up
            if self._yield_to_stt.gave_up:
                # 늦은 회의록이 없는 회의록보다 낫다. 포기했다는 사실은 남긴다 —
                # 자주 켜지면 장비가 모자란다는 뜻이고 증설 근거가 된다.
                log.warning(
                    "STT 양보 상한 초과 — 그대로 진행한다",
                    extra={"session_id": event.session_id, "waited_ms": summary.waited_for_stt_ms},
                )

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
            # 실패도 알린다. 실패를 안 세면 성공률이 항상 100%로 보이고,
            # 그 화면은 아무것도 알려주지 않는다.
            await self._announce(summary)
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
        await self._announce(summary)
        log.info(
            "요약 완료",
            extra={
                "session_id": event.session_id,
                "profile": event.profile.value,
                "status": summary.status.value,
                "latency_ms": summary.latency_ms,
            },
        )

    async def _announce(self, summary: Summary) -> None:
        """끝났다는 **사실**만 스트림에 낸다 — 본문은 싣지 않는다.

        회의록은 개인정보 밀도가 가장 높은 산출물이고, 스트림은 여러 블록이
        함께 읽는 자리다. 본문이 필요하면 권한과 감사가 걸린 조회 API를 쓴다.

        성공·실패·빈 세션을 **모두** 낸다. 지나간 이벤트는 아무도 다시 만들어
        주지 않으므로, 여기서 빠뜨린 경우는 통계에서 영영 사라진다.
        """
        await self.bus.publish(
            Topic.SUMMARY_DONE,
            SummaryDone(
                session_id=summary.session_id,
                tenant_id=summary.tenant_id,
                profile=summary.profile,
                status=summary.status,
                latency_ms=summary.latency_ms,
                waited_for_stt_ms=summary.waited_for_stt_ms,
                yield_gave_up=summary.yield_gave_up,
                transcript_chars=summary.transcript_chars,
            ),
        )
