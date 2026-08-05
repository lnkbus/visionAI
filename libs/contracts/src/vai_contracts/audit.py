"""감사 계약 — "누가 언제 무엇에 접근했는가".

금융·공공 조달에서 감사 로그는 있으면 좋은 기능이 아니라 **통과 조건**이다.
그리고 감사 로그의 가치는 "변조되지 않았음을 증명할 수 있는가"에서 나온다.
지울 수 있는 로그는 감사에서 증거로 쓰이지 못한다.

그래서 저장 형태(:class:`AuditRecord`)에 해시 체인을 넣는다. 한 줄을 고치거나
지우면 그 뒤 모든 레코드의 해시가 어긋나고, 검증 API가 **몇 번째 레코드에서**
끊겼는지까지 짚어 준다.

이벤트를 버스로 흘리는 이유: 감사 기록이 상담 응답 경로에 동기적으로 끼면
감사 저장소가 느려질 때 상담이 느려진다. 단, 접근 통제에 관한 기록
(:attr:`AuditAction.PII_REVEAL` 등)은 **기록 성공 후 열람을 허용**해야 하므로
호출부가 동기 API를 쓴다 — 어느 쪽인지는 호출부가 판단한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from vai_contracts.events import BaseEvent


class AuditAction(StrEnum):
    """감사 대상 행위. 문자열을 직접 만들지 않고 이 목록만 쓴다 —
    자유 문자열을 허용하면 오타로 감사 조회에서 빠지는 기록이 생긴다."""

    LOGIN = "login"
    LOGOUT = "logout"
    ACCESS_DENIED = "access_denied"

    SESSION_START = "session.start"
    SESSION_END = "session.end"

    PII_REVEAL = "pii.reveal"
    """마스킹된 원문 열람. 감사에서 가장 먼저 확인하는 항목이다."""

    RECORDING_PLAY = "recording.play"

    CALL_VIEW = "call.view"
    """상담·회의 이력 열람. 담긴 것이 마스킹본이라 ``pii.reveal``과 구분한다 —
    둘을 같은 이름으로 남기면 실제 원문 열람이 일상 조회에 묻힌다."""

    CALL_DELETE = "call.delete"
    """이력 파기. 지운 사실이 기록되지 않으면 "지웠다"도 "누가 지웠나"도
    증명할 수 없다."""

    EXPORT = "export"
    """회의록·녹취 반출. 유출 사고의 조사 시작점이다."""

    KB_INGEST = "kb.ingest"
    KB_DELETE = "kb.delete"
    RULE_PUBLISH = "rule.publish"
    CONFIG_CHANGE = "config.change"
    LICENSE_INSTALL = "license.install"
    KEY_ROTATE = "key.rotate"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    """거부된 시도야말로 감사에서 중요하다. 성공만 남기면 침해 시도가 안 보인다."""

    ERROR = "error"


class AuditEvent(BaseEvent):
    """``audit.log`` — 감사 기록 1건.

    세션 밖에서 일어나는 행위(로그인, 라이선스 설치)도 담으므로
    ``session_id``/``tenant_id``는 선택이다.
    """

    session_id: str = ""
    tenant_id: str = ""

    actor: str
    """행위자 식별자. 사람이면 사용자 ID, 시스템이면 블록 ID."""

    actor_ip: str = ""
    action: AuditAction
    resource: str = ""
    """대상 식별자(문서 ID, 세션 ID, 룰셋 버전 등)."""

    outcome: AuditOutcome = AuditOutcome.SUCCESS
    block_id: str = ""
    detail: dict[str, str] = Field(default_factory=dict)
    """부가 정보. **원문 PII를 넣지 않는다** — 감사 로그가 유출 경로가 된다."""


class AuditRecord(BaseModel):
    """저장된 감사 레코드. 해시 체인의 한 고리다."""

    seq_no: int
    """1부터 증가. 번호가 비면 삭제가 있었다는 뜻이다."""

    prev_hash: str
    entry_hash: str
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event: AuditEvent


class ChainStatus(BaseModel):
    """무결성 검증 결과."""

    total: int
    intact: bool
    broken_at: int | None = None
    """끊긴 첫 레코드의 ``seq_no``. 조사 범위를 여기서부터 좁힌다."""

    reason: str = ""
    head_hash: str = ""
    """마지막 레코드의 해시. 외부에 정기 공증(예: 일일 출력물)하면
    저장소 전체를 통째로 바꿔치기하는 공격까지 막을 수 있다."""

    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
