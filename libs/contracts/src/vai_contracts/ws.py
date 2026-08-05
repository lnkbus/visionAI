"""WebSocket 통신 프로토콜 ``/v1/audio/stream``.

사양서 §4의 와이어 규격을 그대로 구현한다. 내부 이벤트(:mod:`vai_contracts.events`)와
분리해 두는 이유는, 내부 스키마를 리팩터링해도 고객사 클라이언트가 깨지지 않게
하기 위해서다. 변환은 게이트웨이 한 곳에서만 일어난다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from vai_contracts.events import AssistPopup, FilterResult, KnowledgeHit, SttDelta
from vai_contracts.session import AudioFormat, ChannelRole

# ── Client → Server ──────────────────────────────────────────────────────────


class AudioDataMessage(BaseModel):
    event: Literal["audio_data"] = "audio_data"
    session_id: str
    format: AudioFormat = AudioFormat.PCM_16K
    channel: ChannelRole = ChannelRole.CUSTOMER
    audio_base64: str


class CloseSessionMessage(BaseModel):
    event: Literal["close_session"] = "close_session"
    session_id: str


InboundMessage = AudioDataMessage | CloseSessionMessage


# ── Server → Client ──────────────────────────────────────────────────────────


class SttResultPayload(BaseModel):
    speaker: ChannelRole | str
    text: str
    is_final: bool
    confidence: float = 0.0
    start_ms: int = 0
    """화자 라벨(SPK-DIA)과 이 자막 줄을 맞추는 키. 회의록 화면이 쓴다."""


class CompliancePayload(BaseModel):
    pii_masked: bool = False
    matched_rules: list[str] = Field(default_factory=list)
    """룰 ID 목록. 사양서 §3 모듈 2의 ``script_matches`` 형태."""


class AgentAssistUpdate(BaseModel):
    """서버가 상담원 화면으로 내보내는 통합 이벤트.

    STT 결과만 나온 시점에도 즉시 전송하고(자막 지연 최소화), 팝업이 뒤늦게
    준비되면 같은 ``event``를 팝업만 채워 한 번 더 보낸다. 클라이언트는
    비어 있는 필드를 무시하면 된다.
    """

    event: Literal["agent_assist_update"] = "agent_assist_update"
    session_id: str
    stt_result: SttResultPayload | None = None
    compliance: CompliancePayload | None = None
    knowledge_popup: list[KnowledgeHit] = Field(default_factory=list)

    @classmethod
    def from_stt(cls, delta: SttDelta) -> AgentAssistUpdate:
        return cls(
            session_id=delta.session_id,
            stt_result=SttResultPayload(
                speaker=delta.speaker_id or delta.channel,
                text=delta.text,
                is_final=delta.is_final,
                confidence=delta.confidence,
                start_ms=delta.start_ms,
            ),
        )

    @classmethod
    def from_filter(cls, result: FilterResult) -> AgentAssistUpdate:
        return cls(
            session_id=result.session_id,
            stt_result=SttResultPayload(
                speaker=result.speaker_id or result.channel,
                text=result.clean_text,
                is_final=result.is_final,
                start_ms=result.start_ms,
            ),
            compliance=CompliancePayload(
                pii_masked=result.pii_masked,
                matched_rules=[r.rule_id for r in result.matched_rules],
            ),
        )

    @classmethod
    def from_popup(cls, popup: AssistPopup) -> AgentAssistUpdate:
        return cls(session_id=popup.session_id, knowledge_popup=popup.hits)


class ErrorMessage(BaseModel):
    event: Literal["error"] = "error"
    session_id: str | None = None
    code: str
    message: str


OutboundMessage = AgentAssistUpdate | ErrorMessage
