"""CORE-SEC 워커 — ``audit.log`` → 해시 체인 저장.

버스를 경유하는 비동기 경로다. 여기가 밀려도 상담은 계속 돈다. Redis Streams는
ack 전까지 메시지를 보존하므로 이 블록이 잠깐 죽어도 감사 기록이 사라지지 않는다.
"""

from __future__ import annotations

import logging

from vai_common.bus import EventBus
from vai_common.worker import BlockWorker
from vai_contracts.audit import AuditEvent
from vai_contracts.topics import Topic
from vai_core_sec.chain import AuditChain

log = logging.getLogger(__name__)
BLOCK_ID = "CORE-SEC"


class AuditWorker(BlockWorker[AuditEvent]):
    block_id = BLOCK_ID
    source_topic = Topic.AUDIT_LOG
    source_model = AuditEvent

    def __init__(self, bus: EventBus, chain: AuditChain, *, group: str, consumer: str) -> None:
        super().__init__(bus, group=group, consumer=consumer)
        self._chain = chain

    async def handle(self, event: AuditEvent) -> None:
        await self._chain.append(event)
