"""요약 계약 — 세션 종료 후 배치 산출물.

AICC와 회의록은 요약 형태가 다르다. AICC는 상담 카테고리 분류 + 표준 양식이
필요하고(상담 이력 시스템에 적재되므로), 회의록은 안건·결정사항·Action Item
구조가 필요하다. 하나의 타입에 억지로 담지 않고 프로파일별로 나눈다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from vai_contracts.session import SessionProfile


class SummaryStatus(StrEnum):
    PENDING = "pending"
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

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
