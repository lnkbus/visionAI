"""감사 기록 발신 도우미.

블록이 감사 이벤트를 보내는 방법을 한 곳으로 모은다. 각 블록이 직접 토픽
문자열을 만들면 필드 이름이 조금씩 갈라지고, 그 순간 감사 조회에서 빠지는
기록이 생긴다.

**발신 실패가 업무를 멈추지 않는다.** 감사 저장소가 잠깐 죽었다고 상담이
끊기면 가용성이 감사 요구에 인질로 잡힌다. 다만 접근 통제에 관한 기록
(PII 원문 열람 등)은 반대다 — 그쪽은 CORE-SEC의 동기 API로 **기록 성공 후**
열람을 허용해야 하며, 이 함수를 쓰지 않는다.
"""

from __future__ import annotations

import logging

from vai_common.bus import EventBus
from vai_contracts.audit import AuditAction, AuditEvent, AuditOutcome
from vai_contracts.topics import Topic

log = logging.getLogger(__name__)


async def emit(
    bus: EventBus,
    *,
    action: AuditAction,
    actor: str,
    block_id: str = "",
    resource: str = "",
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    session_id: str = "",
    tenant_id: str = "",
    actor_ip: str = "",
    detail: dict[str, str] | None = None,
) -> None:
    """감사 이벤트를 버스로 흘린다. 실패해도 예외를 올리지 않는다."""
    event = AuditEvent(
        session_id=session_id,
        tenant_id=tenant_id,
        actor=actor,
        actor_ip=actor_ip,
        action=action,
        resource=resource,
        outcome=outcome,
        block_id=block_id,
        detail=detail or {},
    )
    try:
        await bus.publish(Topic.AUDIT_LOG, event)
    except Exception:
        # 로그로는 반드시 남긴다. 감사 저장소가 못 받았다는 사실 자체가
        # 조사 대상이므로 흔적 없이 사라지면 안 된다.
        log.exception(
            "감사 이벤트 발신 실패",
            extra={"action": action.value, "actor": actor, "resource": resource},
        )
