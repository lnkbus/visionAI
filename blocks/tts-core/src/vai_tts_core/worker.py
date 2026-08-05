"""TTS-CORE 워커 — ``tts.request`` → ``tts.audio``, ``tts.cancel`` 수신.

**취소를 별도 태스크로 받는다.** 합성 워커가 요청을 처리하는 동안 같은 루프에서
취소를 기다리면, 지금 말하고 있는 그 발화를 끊을 수 없다. 끼어들기는 발화
**도중에** 들어오는 것이 정상이므로 두 경로를 분리한다.

취소를 Pub/Sub이 아니라 스트림으로 받는 이유도 같다 — 유실되면 봇이 멈추지
않는다. 화면 갱신 한 번 빠지는 것과 무게가 다르다.
"""

from __future__ import annotations

import logging

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.speech import SpeechCancel, SpeechChunk, SynthesisRequest
from vai_contracts.topics import Topic
from vai_tts_core.readings import ReadingCache
from vai_tts_core.synth import Synthesizer, TurnStats

log = logging.getLogger(__name__)
BLOCK_ID = "TTS-CORE"


class SynthesisWorker(BlockWorker[SynthesisRequest]):
    block_id = BLOCK_ID
    source_topic = Topic.TTS_REQUEST
    source_model = SynthesisRequest

    def __init__(
        self,
        bus: EventBus,
        synthesizer: Synthesizer,
        *,
        group: str,
        consumer: str,
        readings: ReadingCache | None = None,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._synth = synthesizer
        self._readings = readings

    async def handle(self, event: SynthesisRequest) -> None:
        stats = TurnStats()
        # 사전은 테넌트별이다. 고유명사 읽기는 고객사마다 다르고, 한 장비에
        # 여러 테넌트가 올라가는 SaaS 구성에서 섞이면 남의 회사 용어로 읽는다.
        readings = await self._readings.readings(event.tenant_id) if self._readings else None
        async for segment, turn_stats in self._synth.synthesize(
            event.session_id, event.turn_id, event.text, event.voice, readings=readings
        ):
            stats = turn_stats
            await self.bus.publish(
                Topic.TTS_AUDIO,
                SpeechChunk(
                    session_id=event.session_id,
                    tenant_id=event.tenant_id,
                    seq=event.seq,
                    turn_id=event.turn_id,
                    seq_in_turn=segment.seq,
                    pcm=segment.pcm,
                    sample_rate=segment.sample_rate,
                    encoding=segment.encoding,
                    text=segment.text,
                    is_final=segment.is_final,
                    duration_ms=segment.duration_ms,
                ),
            )

        log.info(
            "합성 완료" if not stats.cancelled else "합성 취소됨",
            extra={
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "ttfb_ms": round(stats.ttfb_ms, 1),
                "audio_ms": stats.audio_ms,
                "segments": stats.segments,
            },
        )


class CancelWorker(BlockWorker[SpeechCancel]):
    """끼어들기 수신 전용 워커.

    합성 워커와 **다른 컨슈머 그룹**을 쓴다. 같은 그룹이면 취소 메시지가
    합성 중인 워커의 큐 뒤에 줄을 서고, 발화가 끝난 뒤에야 도착한다 —
    그때 취소하는 것은 취소가 아니다.
    """

    block_id = BLOCK_ID
    source_topic = Topic.TTS_CANCEL
    source_model = SpeechCancel

    def __init__(
        self, bus: EventBus, synthesizer: Synthesizer, *, group: str, consumer: str
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._synth = synthesizer

    async def handle(self, event: SpeechCancel) -> None:
        cancelled = self._synth.registry.cancel(event.session_id, event.turn_id)
        log.info(
            "끼어들기",
            extra={
                "session_id": event.session_id,
                "turn_id": event.turn_id or "*",
                "reason": event.reason,
                "cancelled_turns": cancelled,
            },
        )
