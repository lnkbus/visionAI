"""FLT-MICRO 워커 — ``stt.delta`` → ``filter.clean``.

이 블록이 붙는 순간부터 화면에 흐르는 자막은 **마스킹본**이다. STT-CORE의
UI 직송 경로(``VAI_STT_PUBLISH_UI``)를 끄고 여기서만 내보내야, 마스킹 전
텍스트가 화면에 스치는 일이 없다.
"""

from __future__ import annotations

import logging

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.events import FilterResult, SttDelta
from vai_contracts.topics import Topic
from vai_contracts.ws import AgentAssistUpdate
from vai_flt_micro.filter import MicroComplianceFilter

log = logging.getLogger(__name__)
BLOCK_ID = "FLT-MICRO"


class FilterWorker(BlockWorker[SttDelta]):
    block_id = BLOCK_ID
    source_topic = Topic.STT_DELTA
    source_model = SttDelta
    latency_budget_ms = 10.0
    """사양서 §3 모듈 2의 Fast-Path 예산. 초과하면 정규식이 잘못 짜인 것이다."""

    def __init__(
        self,
        bus: EventBus,
        filters: MicroComplianceFilter,
        *,
        group: str,
        consumer: str,
        publish_ui: bool = True,
    ) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._filter = filters
        self._publish_ui = publish_ui

    async def handle(self, event: SttDelta) -> None:
        outcome = self._filter.process_text(event.text)

        result = FilterResult(
            session_id=event.session_id,
            tenant_id=event.tenant_id,
            seq=event.seq,
            channel=event.channel,
            clean_text=outcome.clean_text,
            pii_masked=outcome.pii_masked,
            pii_types=outcome.pii_types,
            matched_rules=outcome.matched_rules,
            is_final=event.is_final,
            speaker_id=event.speaker_id,
        )
        await self.bus.publish(Topic.FILTER_CLEAN, result)

        if outcome.pii_masked:
            # 무엇이 걸렸는지만 남기고 값 자체는 절대 남기지 않는다.
            log.info(
                "PII 마스킹",
                extra={
                    "session_id": event.session_id,
                    "pii_types": ",".join(outcome.pii_types),
                },
            )
        for rule in outcome.matched_rules:
            if rule.severity != "info":
                log.warning(
                    "컴플라이언스 룰 매칭",
                    extra={
                        "session_id": event.session_id,
                        "rule_id": rule.rule_id,
                        "severity": rule.severity,
                    },
                )

        if self._publish_ui:
            await self.bus.publish_ui(event.session_id, AgentAssistUpdate.from_filter(result))
