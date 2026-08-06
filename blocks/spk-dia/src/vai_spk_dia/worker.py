"""SPK-DIA 워커 — ``audio.segment`` → ``speaker.label``.

**MEETING 프로파일에서만 동작한다.** AICC는 채널(고객/상담원)로 이미 화자가
갈려 있어 화자분리가 필요 없고, 오히려 GPU만 쓴다.

자막 경로와 분리해 병렬로 돈다. 화자분리는 인식보다 느리고 정확도도 낮으므로,
자막이 라벨을 기다리면 회의 자막이 통째로 늦어진다. 라벨은 뒤따라 붙는다.
"""

from __future__ import annotations

import logging

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import AudioSegment, SpeakerLabel
from vai_contracts.session import SessionProfile
from vai_contracts.topics import Topic
from vai_spk_dia.diarizer import DiarizerConfig, OnlineDiarizer
from vai_spk_dia.embedding import BaseSpeakerEmbedder

log = logging.getLogger(__name__)
BLOCK_ID = "SPK-DIA"


class DiarizationWorker(BlockWorker[AudioSegment]):
    block_id = BLOCK_ID
    source_topic = Topic.AUDIO_SEGMENT
    source_model = AudioSegment
    latency_budget_ms = 500.0
    """자막 경로와 병렬이라 여유가 있다. 이걸 넘으면 임베더가 과하다는 뜻."""

    def __init__(
        self,
        bus: EventBus,
        embedder: BaseSpeakerEmbedder,
        *,
        group: str,
        consumer: str,
        config: DiarizerConfig | None = None,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._embedder = embedder
        self._config = config or DiarizerConfig()
        self._sessions: dict[str, OnlineDiarizer] = {}
        self._seq: dict[str, int] = {}

    def diarizer(self, session_id: str) -> OnlineDiarizer:
        return self._sessions.setdefault(session_id, OnlineDiarizer(config=self._config))

    def _next_seq(self, session_id: str) -> int:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        return seq

    async def handle(self, event: AudioSegment) -> None:
        if event.profile is not SessionProfile.MEETING:
            return
        if not event.is_final:
            # 중간 구간은 같은 오디오가 다시 오므로, 임베딩을 두 번 뽑고
            # 중심을 두 번 갱신하게 된다. 확정 구간만 쓴다.
            return

        embedding = await self._embedder.embed(event.pcm, event.sample_rate)
        if embedding is None:
            # 너무 짧거나 무음. 근거 없는 라벨을 붙이면 회의록에 없는
            # 참석자가 생긴다.
            return

        # **말한 길이로 판정한다.** duration_ms 에는 앞뒤 패딩(200ms)과
        # 행오버(400ms)가 붙어 있어서, 실제 발화가 50ms 인 기침도 660ms 로
        # 보인다 — 그 값으로 "짧아서 새 화자로 안 친다"를 판정하면 조건이
        # 한 번도 안 걸린다. 회의 시작 몇 초 만에 잡음에서 화자가 일곱 명
        # 생겼고, 각각 "1초·1회" 였다.
        #
        # 0 이면 옛 AUD-VAD 가 안 채운 것이다. 그때만 물러선다.
        speech_ms = event.speech_ms or event.duration_ms
        assignment = self.diarizer(event.session_id).assign(embedding, speech_ms)
        if assignment is None:
            return

        await self.bus.publish(
            Topic.SPEAKER_LABEL,
            SpeakerLabel(
                session_id=event.session_id,
                tenant_id=event.tenant_id,
                seq=self._next_seq(event.session_id),
                channel=event.channel,
                speaker_id=assignment.speaker_id,
                start_ms=event.start_ms,
                duration_ms=event.duration_ms,
                confidence=round(assignment.confidence, 4),
                is_new_speaker=assignment.is_new,
            ),
        )

        if assignment.is_new:
            log.info(
                "참석자 추가",
                extra={
                    "session_id": event.session_id,
                    "speaker_id": assignment.speaker_id,
                    "speaker_count": self.diarizer(event.session_id).speaker_count,
                },
            )

    def release_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._seq.pop(session_id, None)
