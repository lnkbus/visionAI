"""STT-CORE 워커 — ``audio.segment`` → ``stt.delta``.

FLT-MICRO(Wave 3)가 붙기 전까지는 UI가 자막을 볼 수 있도록 STT 결과를
UI 채널로도 직접 내보낸다. 필터 블록이 들어오면 이 직송 경로는
``publish_ui=False``로 끄고 필터 결과만 화면에 흐르게 한다 —
마스킹 전 텍스트가 화면에 스치는 일이 없어야 하기 때문이다.
"""

from __future__ import annotations

import logging

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import AudioSegment, SttDelta
from vai_contracts.topics import Topic
from vai_contracts.ws import AgentAssistUpdate
from vai_stt_core.adapters.base import BaseSTTAdapter

log = logging.getLogger(__name__)
BLOCK_ID = "STT-CORE"


class SttWorker(BlockWorker[AudioSegment]):
    block_id = BLOCK_ID
    source_topic = Topic.AUDIO_SEGMENT
    source_model = AudioSegment
    latency_budget_ms = 400.0
    """구간 하나의 인식 시간. 실시간 배수(RTF) < 1을 유지해야 큐가 밀리지 않는다."""

    def __init__(
        self,
        bus: EventBus,
        adapter: BaseSTTAdapter,
        *,
        group: str,
        consumer: str,
        publish_ui: bool = True,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._adapter = adapter
        self._publish_ui = publish_ui
        self._seq: dict[str, int] = {}

    def _next_seq(self, session_id: str) -> int:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        return seq

    async def handle(self, event: AudioSegment) -> None:
        async for result in self._adapter.transcribe_stream(event.pcm, event.sample_rate):
            text = result["text"].strip()
            if not text:
                continue

            delta = SttDelta(
                session_id=event.session_id,
                tenant_id=event.tenant_id,
                seq=self._next_seq(event.session_id),
                channel=event.channel,
                text=text,
                # 구간이 중간(interim)이면 인식 결과도 확정으로 볼 수 없다.
                is_final=event.is_final and result["is_final"],
                confidence=result["confidence"],
                speaker_id=result.get("speaker_id"),
                start_ms=event.start_ms,
                duration_ms=event.duration_ms,
                language=result.get("language", "ko"),
            )
            await self.bus.publish(Topic.STT_DELTA, delta)

            if self._publish_ui:
                await self.bus.publish_ui(event.session_id, AgentAssistUpdate.from_stt(delta))

    def release_session(self, session_id: str) -> None:
        self._seq.pop(session_id, None)
