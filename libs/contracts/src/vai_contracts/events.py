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
    """구간의 실제 길이(패딩·행오버 포함). 타임라인 정렬은 이 값을 쓴다."""

    is_final: bool = True
    """False면 발화가 계속 중인 중간 구간(부분 인식용)."""

    speech_ms: int = 0
    """이 구간에서 **음성으로 판정된 프레임만** 합친 길이.

    ``duration_ms``와 나눠 둔 이유가 이 계약에서 가장 중요한 구분이다.
    앞뒤로 패딩(200ms)과 행오버(400ms)가 붙으므로, **실제 발화가 50ms 여도
    구간 길이는 660ms 가 된다.** 그 값으로 "너무 짧아서 잡음"을 거르면
    아무것도 안 걸러진다.

    AUD-VAD 는 이 값을 계산해 놓고 이벤트에 안 실었고, SPK-DIA 는 없는 줄
    모르고 ``duration_ms`` 로 걸렀다. 그래서 회의 시작 몇 초 만에 잡음에서
    화자가 일곱 명 생겼다 — 각각 "1초·1회", "2초·1회" 였다. 회의록에
    참석하지 않은 사람이 생기고, 그 사실은 아무도 못 알아본다.

    0 이면 "producer 가 안 채웠다"는 뜻이다. 받는 쪽은 그때만 ``duration_ms``
    로 물러선다 — 옛 producer 와 섞여 도는 구간을 위해서다.
    """


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


class SpeakerLabel(BaseEvent):
    """``speaker.label`` — 발화 구간의 화자 식별 결과.

    ``(session_id, channel, start_ms)``로 :class:`SttDelta`와 맞춘다. STT가
    구간의 ``start_ms``를 그대로 옮기므로 이 조합이 결합 키가 된다.
    """

    channel: ChannelRole
    speaker_id: str
    """세션 내 화자 식별자. ``speaker_1`` 형태이며, 실명 매핑은 UI에서 한다."""

    start_ms: int
    duration_ms: int
    confidence: float = 0.0
    """군집 중심과의 유사도. 낮으면 화자가 섞였을 수 있다는 신호다."""

    is_new_speaker: bool = False


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
    start_ms: int = 0
    """발화 구간 시작 시각. SPK-DIA의 화자 라벨과 맞추는 결합 키다 —
    여기서 끊기면 회의록 화면이 자막에 화자를 붙일 방법이 없다."""

    duration_ms: int = 0


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
