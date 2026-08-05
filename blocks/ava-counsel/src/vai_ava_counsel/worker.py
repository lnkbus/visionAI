"""AVA-COUNSEL 워커 — ``tts.audio`` → ``avatar.track``.

합성 결과를 **받은 뒤에** 입 모양을 만든다. 미리 만들면 실제 합성 길이와
어긋나고, 그 오차는 문장이 길어질수록 누적돼 끝에서는 눈에 띄게 벌어진다.

오디오와 같은 ``turn_id``/``seq_in_turn``을 쓴다. 클라이언트가 둘을 짝지어
재생하므로 식별자가 어긋나면 동기가 통째로 깨진다.
"""

from __future__ import annotations

import logging

from vai_ava_counsel import BLOCK_ID
from vai_ava_counsel.viseme import build_track
from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.avatar import AvatarState, AvatarTrack, VisemeFrame
from vai_contracts.speech import SpeechChunk
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)


class AvatarWorker(BlockWorker[SpeechChunk]):
    block_id = BLOCK_ID
    source_topic = Topic.TTS_AUDIO
    source_model = SpeechChunk
    latency_budget_ms = 20.0
    """입 모양 계산은 순수 연산이라 빨라야 한다. 여기서 밀리면 오디오가
    먼저 도착해 입이 뒤늦게 움직인다 — 사람은 그 어긋남을 즉시 알아챈다."""

    def __init__(self, bus: EventBus, *, group: str, consumer: str) -> None:
        super().__init__(bus, group=group, consumer=consumer)

    async def handle(self, event: SpeechChunk) -> None:
        if event.is_final:
            # 발화 끝. 입을 다물고 듣는 자세로 돌아간다 — 마지막 입 모양이
            # 남아 있으면 아바타가 입을 벌린 채 굳어 있는 것처럼 보인다.
            await self._publish(event, frames=[], state=AvatarState.LISTENING)
            return

        frames = build_track(event.text, event.duration_ms)
        await self._publish(event, frames=frames, state=AvatarState.SPEAKING)

    async def _publish(
        self, event: SpeechChunk, *, frames: list[VisemeFrame], state: AvatarState
    ) -> None:
        await self.bus.publish(
            Topic.AVATAR_TRACK,
            AvatarTrack(
                session_id=event.session_id,
                tenant_id=event.tenant_id,
                seq=event.seq,
                turn_id=event.turn_id,
                seq_in_turn=event.seq_in_turn,
                duration_ms=event.duration_ms,
                text=event.text,
                frames=frames,
                state=state,
            ),
        )
