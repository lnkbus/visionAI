"""요약 계약 — 세션 종료 후 배치 산출물.

AICC와 회의록은 요약 형태가 다르다. AICC는 상담 카테고리 분류 + 표준 양식이
필요하고(상담 이력 시스템에 적재되므로), 회의록은 안건·결정사항·Action Item
구조가 필요하다. 하나의 타입에 억지로 담지 않고 프로파일별로 나눈다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from vai_contracts.events import BaseEvent
from vai_contracts.session import SessionProfile


class SummaryStatus(StrEnum):
    PENDING = "pending"

    WAITING = "waiting"
    """실시간 경로(STT)에 양보하며 차례를 기다리는 중.

    한 장비에서 STT와 요약 sLLM이 메모리를 다투면 **먼저 무너지는 것이 실시간
    자막**이다. 회의 중 자막이 끊기는 것과 회의록이 3분 늦는 것은 비용이 다르다.

    이 상태가 화면에 보여야 한다. 아무 표시가 없으면 사용자는 요약이 실패했다고
    생각하고, 그때 할 수 있는 일이 없다."""

    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class ActionItem(BaseModel):
    """회의록 Action Item."""

    text: str
    owner: str = ""
    due: str = ""
    """자유 표기("다음 주까지"). 정규화는 사람이 확인 후 한다 — LLM이 추측한
    날짜를 그대로 일정에 넣으면 사고가 난다."""


class AiccSummary(BaseModel):
    """상담 표준 요약. 상담 이력 시스템 적재용."""

    category: str = ""
    """상담 유형 분류. 테넌트가 정의한 코드 체계를 따른다."""

    subcategory: str = ""
    customer_request: str = ""
    agent_response: str = ""
    resolution: str = ""
    follow_up: str = ""
    keywords: list[str] = Field(default_factory=list)


class MeetingSummary(BaseModel):
    """회의록 요약."""

    title: str = ""
    agenda: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)


class Summary(BaseModel):
    """요약 산출물 1건.

    모델·프롬프트 버전을 함께 남긴다. 금융 감사에서 "이 요약은 어떤 모델로
    언제 만들어졌나"에 답할 수 있어야 하고, 프롬프트를 바꿨을 때 품질 변화의
    원인을 추적할 수 있어야 한다.
    """

    session_id: str
    tenant_id: str
    profile: SessionProfile
    status: SummaryStatus = SummaryStatus.PENDING
    aicc: AiccSummary | None = None
    meeting: MeetingSummary | None = None
    transcript_chars: int = 0
    model: str = ""
    prompt_version: str = ""
    latency_ms: int = 0
    error: str | None = None
    edited: bool = False
    """사람이 수정했는지. 수정률이 곧 요약 품질 지표다(docs/06 §5)."""

    waited_for_stt_ms: int = 0
    """실시간 경로에 양보하며 기다린 시간. 자주 커지면 장비가 모자란다는 뜻이고,
    그건 증설 근거가 된다."""

    yield_gave_up: bool = False
    """상한을 넘겨 양보를 포기했는가. 실패가 아니라 기록이다."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SummaryDone(BaseEvent):
    """``summary.done`` — 요약이 끝났다는 사실.

    요약 본문을 싣지 않는다. 회의록은 개인정보 밀도가 가장 높은 산출물이고,
    스트림은 여러 블록이 함께 읽는 자리다. 본문이 필요한 쪽은 LLM-SUM의 조회
    API를 쓰면 되며, 그쪽에는 권한과 감사 기록이 걸려 있다.

    통계·운영 화면은 여기 실린 숫자만으로 충분하다.
    """

    profile: SessionProfile
    status: SummaryStatus = SummaryStatus.PENDING
    latency_ms: int = 0
    waited_for_stt_ms: int = 0
    yield_gave_up: bool = False
    transcript_chars: int = 0
