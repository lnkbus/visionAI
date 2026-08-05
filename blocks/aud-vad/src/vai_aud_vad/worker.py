"""AUD-VAD 워커 — ``audio.in`` → ``audio.segment``."""

from __future__ import annotations

import logging

from vai_aud_vad.adapters.base import BaseVadAdapter
from vai_aud_vad.segmenter import SegmenterConfig, SpeechSegmenter
from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import AudioChunk, AudioSegment
from vai_contracts.session import SessionProfile
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)
BLOCK_ID = "AUD-VAD"

# 프로파일별 분할 정책. AICC는 응답 속도가, 회의록은 문장 완결성이 우선이다.
PROFILE_CONFIG: dict[SessionProfile, SegmenterConfig] = {
    SessionProfile.AICC: SegmenterConfig(
        hangover_ms=350, interim_interval_ms=600, max_segment_ms=12_000
    ),
    SessionProfile.MEETING: SegmenterConfig(
        hangover_ms=700, interim_interval_ms=1500, max_segment_ms=25_000
    ),
}


class VadWorker(BlockWorker[AudioChunk]):
    block_id = BLOCK_ID
    source_topic = Topic.AUDIO_IN
    source_model = AudioChunk
    latency_budget_ms = 50.0
    """청크 하나를 자르는 데 드는 시간. 여기서 밀리면 뒤 단계는 손쓸 수 없다."""

    def __init__(self, bus: EventBus, vad: BaseVadAdapter, *, group: str, consumer: str) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._vad = vad
        self._segmenters: dict[tuple[str, int], SpeechSegmenter] = {}
        self._seq: dict[str, int] = {}

    def _segmenter(
        self, session_id: str, sample_rate: int, profile: SessionProfile
    ) -> SpeechSegmenter:
        key = (session_id, sample_rate)
        segmenter = self._segmenters.get(key)
        if segmenter is None:
            segmenter = SpeechSegmenter(
                self._vad, sample_rate, PROFILE_CONFIG.get(profile, SegmenterConfig())
            )
            self._segmenters[key] = segmenter
        return segmenter

    def _next_seq(self, session_id: str) -> int:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        return seq

    async def handle(self, event: AudioChunk) -> None:
        segmenter = self._segmenter(event.session_id, event.sample_rate, event.profile)
        stream_key = f"{event.session_id}:{event.channel.value}"

        for segment in segmenter.push(stream_key, event.pcm):
            if not segmenter.is_meaningful(segment):
                continue
            await self.bus.publish(
                Topic.AUDIO_SEGMENT,
                AudioSegment(
                    session_id=event.session_id,
                    tenant_id=event.tenant_id,
                    seq=self._next_seq(event.session_id),
                    channel=event.channel,
                    sample_rate=event.sample_rate,
                    pcm=segment.pcm,
                    profile=event.profile,
                    start_ms=segment.start_ms,
                    duration_ms=segment.duration_ms,
                    is_final=segment.is_final,
                ),
            )

    def release_session(self, session_id: str) -> None:
        """세션 종료 시 버퍼를 정리한다. 호출하지 않으면 메모리가 샌다."""
        for key in [k for k in self._segmenters if k[0] == session_id]:
            del self._segmenters[key]
        self._seq.pop(session_id, None)
