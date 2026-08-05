"""저작·학습 계약 — 고객사가 스스로 운영하기 위한 규격.

온프레미스 B2B에서 이 계층이 없으면 룰 한 줄, 상품명 하나 바꾸는 데도 공급사
인력이 투입된다. 유지보수 원가가 라이선스 수익을 잠식하므로, 저작 도구는
선택이 아니라 수익성의 전제다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RuleSeverity(StrEnum):
    INFO = "info"
    """필수 고지 등 '했는지 확인'하는 룰."""

    WARNING = "warning"
    VIOLATION = "violation"
    """금지 표현 발화 등 즉시 조치가 필요한 룰."""


class ComplianceRuleSpec(BaseModel):
    """저작 가능한 컴플라이언스 룰 1건."""

    rule_id: str
    pattern: str
    severity: RuleSeverity = RuleSeverity.INFO
    message: str = ""
    enabled: bool = True
    description: str = ""
    """운영자가 나중에 '이 룰이 왜 있더라'를 알 수 있게 하는 메모."""

    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by: str = ""


class RuleSet(BaseModel):
    """테넌트별 룰셋. 버전을 올려 배포하고 문제 시 되돌린다."""

    tenant_id: str
    version: int = 1
    rules: list[ComplianceRuleSpec] = Field(default_factory=list)
    published: bool = False
    """False면 저작 중(초안). 상담 파이프라인은 published 버전만 적용한다."""

    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuleTestRequest(BaseModel):
    """``POST /internal/v1/rules/test`` — 저작 중 즉시 검증.

    룰을 배포한 뒤에야 오작동을 알게 되면 이미 상담 로그가 오염된 뒤다.
    저작 화면에서 문장을 넣어 바로 확인할 수 있어야 한다.
    """

    tenant_id: str
    text: str
    rules: list[ComplianceRuleSpec] | None = None
    """미지정 시 저장된 초안 룰셋을 쓴다."""


class RuleTestResult(BaseModel):
    clean_text: str
    pii_masked: bool = False
    pii_types: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0


class LexiconEntry(BaseModel):
    """STT 커스텀 사전 항목.

    금융 상품명·사명·전문용어는 범용 STT가 거의 확실히 틀린다. "무배당 OO보험"이
    "무배당 오오보험"으로 인식되면 검색도 요약도 전부 어긋난다.
    """

    surface: str
    """정답 표기. 예: "무배당 행복플러스보험"."""

    variants: list[str] = Field(default_factory=list)
    """실제로 관측된 오인식 표기. 예: ["무배당 행복 플러스 보험", "무배담 행복플러스"]"""

    category: str = "product"
    """product / company / jargon / person"""

    enabled: bool = True


class Lexicon(BaseModel):
    tenant_id: str
    version: int = 1
    entries: list[LexiconEntry] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GoldenCase(BaseModel):
    """평가셋 1건. 검색·요약 품질의 회귀 기준이다."""

    case_id: str
    query: str
    expected_doc_ids: list[str] = Field(default_factory=list)
    """이 질의에 대해 반드시 상위에 나와야 하는 문서."""

    note: str = ""


class EvalRequest(BaseModel):
    tenant_id: str
    kb_id: str = "default"
    cases: list[GoldenCase] = Field(default_factory=list)
    top_k: int = 3


class EvalCaseResult(BaseModel):
    case_id: str
    query: str
    hit: bool
    """기대 문서가 상위 ``top_k`` 안에 들었는지."""

    rank: int | None = None
    returned_doc_ids: list[str] = Field(default_factory=list)
    latency_ms: int = 0


class EvalReport(BaseModel):
    """평가 실행 리포트. 운영자가 룰·사전·청킹을 바꾼 뒤 효과를 확인하는 근거."""

    total: int = 0
    hits: int = 0
    recall_at_k: float = 0.0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    results: list[EvalCaseResult] = Field(default_factory=list)
    ran_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PopupFeedback(BaseModel):
    """상담원이 팝업을 썼는지 여부. 품질 개선 루프의 입력.

    채택률이 낮은 질의 패턴이 곧 개선 대상이다. 이 신호가 없으면 무엇을 고쳐야
    하는지 알 방법이 상담원 인터뷰밖에 없다.
    """

    session_id: str
    tenant_id: str
    doc_id: str
    query: str
    accepted: bool
    reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FeedbackStats(BaseModel):
    total: int = 0
    accepted: int = 0
    acceptance_rate: float = 0.0
    worst_queries: list[str] = Field(default_factory=list)
    """채택되지 않은 질의 상위 목록 — 다음 개선 작업의 우선순위."""
