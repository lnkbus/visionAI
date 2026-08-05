"""블록 간 이벤트 페이로드.

모든 이벤트는 ``BaseEvent``를 상속해 ``session_id``/``tenant_id``/``seq``를 갖는다.
``seq``는 세션 내 단조 증가 번호로, 순서 복원과 중복 제거에 쓴다.

계약 규칙: 필드 추가는 자유(하위 호환), 필드 삭제·의미 변경은 금지.
깨지는 변경이 필요하면 ``v2`` 모듈을 새로 만든다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from vai_contracts.session import ChannelRole, SessionProfile

SCHEMA_VERSION = 1


class BaseEvent(BaseModel):
    # PCM 본문을 담는 이벤트가 있으므로 bytes는 JSON에서 base64로 왕복시킨다.
    # 기본 동작(utf-8 decode)은 바이너리 오디오에서 즉시 깨진다.
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    schema_version: int = SCHEMA_VERSION
    session_id: str
    tenant_id: str
    seq: int = 0
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AudioChunk(BaseEvent):
    """``audio.in`` — 인입 오디오 청크.

    오디오 본문은 base64가 아니라 bytes로 다룬다. 게이트웨이가 WebSocket
    프레임을 디코드한 직후 한 번만 base64를 벗기고, 이후 파이프라인은
    raw PCM으로 흐른다(청크당 33% 크기 이득 + 디코드 비용 제거).
    """

    channel: ChannelRole
    sample_rate: int
    pcm: bytes
    profile: SessionProfile


class AudioSegment(BaseEvent):
    """``audio.segment`` — VAD가 확정한 발화 구간."""

    channel: ChannelRole
    sample_rate: int
    pcm: bytes
    profile: SessionProfile
    start_ms: int
    duration_ms: int
    is_final: bool = True
    """False면 발화가 계속 중인 중간 구간(부분 인식용)."""


class SttDelta(BaseEvent):
    """``stt.delta`` — 스트리밍 인식 결과.

    필드 구성은 사양서 §3 모듈 1의 ``transcribe_stream`` 반환 형태를 따른다.
    """

    channel: ChannelRole
    text: str
    is_final: bool
    confidence: float = 0.0
    speaker_id: str | None = None
    start_ms: int = 0
    duration_ms: int = 0
    language: str = "ko"


class MatchedRule(BaseModel):
    """컴플라이언스 룰 매칭 1건."""

    rule_id: str
    severity: str = "info"
    message: str = ""


class FilterResult(BaseEvent):
    """``filter.clean`` — 마스킹본 + 룰 체크 결과 (Fast-Path 산출물)."""

    channel: ChannelRole
    clean_text: str
    """마스킹된 텍스트. 이후 단계(LLM/로그/분석)는 이것만 사용한다."""

    pii_masked: bool = False
    pii_types: list[str] = Field(default_factory=list)
    matched_rules: list[MatchedRule] = Field(default_factory=list)
    is_final: bool = True
    speaker_id: str | None = None


class KnowledgeHit(BaseModel):
    """지식 팝업 1건. 사양서 §4 ``knowledge_popup`` 규격."""

    doc_id: str
    title: str
    score: float
    snippet: str
    recommended_answer: str = ""


class AssistPopup(BaseEvent):
    """``assist.popup`` — 상담원 화면에 띄울 추천."""

    query: str
    """SLM Query Extractor가 뽑은 검색 질의."""

    hits: list[KnowledgeHit] = Field(default_factory=list)
    latency_ms: int = 0


class SessionClosed(BaseEvent):
    """``session.closed`` — 배치 요약 트리거."""

    profile: SessionProfile
    duration_ms: int = 0
    utterance_count: int = 0
