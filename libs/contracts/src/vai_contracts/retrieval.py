"""지식 검색 계약 — RAG-KB / RAG-SRCH / TA-ASSIST가 공유하는 규격.

RAG-KB는 문서를 쪼개 색인하고, RAG-SRCH는 질의로 조회하며, TA-ASSIST는
그 결과를 팝업으로 바꾼다. 세 블록이 같은 데이터를 다루므로 형태를 여기서
한 번만 정의한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DocumentStatus(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"


class Document(BaseModel):
    """색인 대상 문서 1건."""

    doc_id: str
    tenant_id: str
    kb_id: str
    title: str
    source: str = ""
    """원본 위치(파일명·URL). 감사 시 '어느 문서로 답했는지' 추적에 쓴다."""

    version: int = 1
    """약관 개정처럼 내용이 바뀌면 올린다. 과거 상담이 어느 판본을 근거로
    했는지 남기려면 버전이 필요하다."""

    status: DocumentStatus = DocumentStatus.PENDING
    chunk_count: int = 0
    error: str | None = None

    warnings: list[str] = Field(default_factory=list)
    """온전하지 않게 읽힌 부분(표 안의 글자, 이미지 페이지, 머리말 등).

    색인은 성공했지만 일부가 빠졌다는 사실을 여기 남긴다. 조용히 넘기면
    "왜 이 조항만 검색이 안 되지"를 아무도 설명하지 못한다."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Chunk(BaseModel):
    """문서 조각. 검색·인용의 최소 단위."""

    chunk_id: str
    doc_id: str
    tenant_id: str
    kb_id: str
    title: str
    text: str
    ordinal: int = 0
    doc_version: int = 1
    metadata: dict[str, str] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    """``POST /internal/v1/search``."""

    tenant_id: str
    kb_id: str
    query: str
    top_k: int = 3
    """최종 반환 개수. 사양서 §3 모듈 3은 상위 10 후보 → 최종 3건."""

    candidate_k: int = 10
    """리랭킹 전 후보 수."""

    min_score: float = 0.0


class ScoredChunk(BaseModel):
    """점수가 매겨진 검색 결과."""

    chunk: Chunk
    score: float
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rerank_score: float | None = None

    @property
    def is_reranked(self) -> bool:
        return self.rerank_score is not None


class SearchResponse(BaseModel):
    hits: list[ScoredChunk] = Field(default_factory=list)
    latency_ms: int = 0
    dense_ms: int = 0
    sparse_ms: int = 0
    rerank_ms: int = 0
    """단계별 지연 — 사양서 예산(검색 100ms, 리랭킹 80ms) 회귀 감시에 쓴다."""


class IngestRequest(BaseModel):
    """``POST /internal/v1/kb/{kb_id}/documents``."""

    tenant_id: str
    title: str
    text: str
    source: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class CompletionRequest(BaseModel):
    """``POST /internal/v1/complete`` — LLM-GW 호출 규격."""

    tenant_id: str
    prompt: str
    system: str = ""
    max_tokens: int = 256
    temperature: float = 0.0
    profile: str = "default"
    """모델 프로파일. ``slm``(질의 추출용 경량) / ``default``(요약용) 등으로
    같은 게이트웨이가 크기가 다른 모델을 나눠 서빙한다."""


class CompletionResponse(BaseModel):
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
