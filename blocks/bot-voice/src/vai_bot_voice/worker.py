"""BOT-VOICE 워커 — ``filter.clean`` → ``tts.request`` + ``bot.turn``.

**마스킹된 텍스트를 받는다.** STT 원문이 아니라 FLT-MICRO를 지난 것을 소비하므로,
봇이 다루는 텍스트에는 이미 PII가 지워져 있다. 봇 로그·시나리오 슬롯에 주민번호가
남는 사고를 구조적으로 막는다.

**최종 인식만 처리한다.** 중간 결과(interim)로 대화를 진행하면 고객이 말하는
도중에 봇이 끼어들어 답한다.
"""

from __future__ import annotations

import asyncio
import logging

from vai_bot_voice.classify import BaseIntentClassifier
from vai_bot_voice.engine import DialogEngine, Utterance, match_intent
from vai_bot_voice.store import DialogStore
from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.dialog import BotTurn, DialogState
from vai_contracts.events import FilterResult
from vai_contracts.speech import SynthesisRequest, VoiceProfile
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)
BLOCK_ID = "BOT-VOICE"

CLASSIFY_BUDGET_MS = 600.0
"""의도 분류에 허용하는 시간.

통화 중이다. 이 시간을 넘기면 분류를 포기하고 되묻는다 — 늦은 정답보다
제때의 되묻기가 낫다. 침묵이 길어지면 고객은 전화가 끊긴 줄 안다."""


class BotWorker(BlockWorker[FilterResult]):
    block_id = BLOCK_ID
    source_topic = Topic.FILTER_CLEAN
    source_model = FilterResult

    def __init__(
        self,
        bus: EventBus,
        store: DialogStore,
        *,
        group: str,
        consumer: str,
        voice: VoiceProfile | None = None,
        classifier: BaseIntentClassifier | None = None,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._store = store
        self._voice = voice or VoiceProfile()
        self._classifier = classifier

    async def handle(self, event: FilterResult) -> None:
        if not event.is_final or not event.clean_text.strip():
            # 중간 결과로 대화를 진행하면 고객이 말하는 도중에 봇이 답한다.
            return

        scenario = await self._store.get_scenario(event.tenant_id)
        if scenario is None:
            # 시나리오가 없으면 이 세션은 봇 세션이 아니다(사람 상담원 구성).
            return

        state = await self._store.get_state(event.session_id)
        engine = DialogEngine(scenario)

        if state is None:
            result = engine.start(event.session_id, event.tenant_id)
        else:
            if state.finished:
                return
            result = engine.reply(
                state,
                event.clean_text,
                intent_name=await self._classify(engine, state, event.clean_text),
            )

        await self._emit(event, result.state, result.text, result.utterances)

    async def _classify(self, engine: DialogEngine, state: DialogState, text: str) -> str:
        """정규식이 놓쳤을 때만 분류기를 부른다.

        비용을 **되묻게 될 발화에서만** 낸다. 정규식이 맞힌 발화까지 모델에
        태우면 저작자가 순서로 표현한 우선순위도 함께 흔들린다.
        """
        node = engine.awaiting(state)
        if self._classifier is None or node is None or match_intent(node, text) is not None:
            return ""
        try:
            chosen = await asyncio.wait_for(
                self._classifier.classify(node, text), CLASSIFY_BUDGET_MS / 1000
            )
        except TimeoutError:
            log.warning(
                "의도 분류 시간 초과 — 되묻기로 진행",
                extra={"session_id": state.session_id, "node": node.node_id},
            )
            return ""
        if chosen:
            log.info(
                "정규식이 놓친 발화를 분류기가 라우팅",
                extra={"session_id": state.session_id, "node": node.node_id, "intent": chosen},
            )
        return chosen or ""

    async def _emit(
        self,
        event: FilterResult,
        state: DialogState | None,
        text: str,
        utterances: list[Utterance],
    ) -> None:
        if state is None:
            return
        await self._store.save_state(state)
        if not text.strip():
            return

        handed_off = any(u.handoff for u in utterances)
        node_id = utterances[-1].node_id if utterances else state.current_node

        await self.bus.publish(
            Topic.TTS_REQUEST,
            SynthesisRequest(
                session_id=state.session_id,
                tenant_id=state.tenant_id,
                seq=event.seq,
                turn_id=state.turn_id,
                text=text,
                voice=self._voice,
            ),
        )
        # 봇이 무슨 말을 했는지 남긴다. 없으면 분쟁 시 아무것도 증명할 수 없다.
        await self.bus.publish(
            Topic.BOT_TURN,
            BotTurn(
                session_id=state.session_id,
                tenant_id=state.tenant_id,
                seq=event.seq,
                turn_id=state.turn_id,
                node_id=node_id,
                text=text,
                slots=dict(state.slots),
                handed_off=handed_off,
            ),
        )

        if handed_off:
            log.info(
                "사람 상담원 연결",
                extra={"session_id": state.session_id, "node": node_id, "slots": len(state.slots)},
            )
