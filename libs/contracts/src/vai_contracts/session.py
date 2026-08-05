"""세션 도메인 모델 — Dual-Profile 처리의 기준값."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class SessionProfile(StrEnum):
    """처리 프로파일. 사양서 §1 Dual-Profile Processing.

    파이프라인 전체가 이 값에 따라 동작을 달리한다: 청크 크기, STT 빔 크기,
    화자분리 여부, 요약 템플릿.
    """

    AICC = "aicc"
    """초저지연. Stereo/RTP, 실시간 감지·팝업."""

    MEETING = "meeting"
    """고정밀. Mono/File, 화자분리·회의록 요약."""


class ChannelRole(StrEnum):
    """오디오 채널의 화자 역할."""

    CUSTOMER = "customer"
    AGENT = "agent"
    PARTICIPANT = "participant"
    """회의 프로파일 — 실제 화자는 SPK-DIA가 부여한다."""


class SessionState(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class AudioFormat(StrEnum):
    PCM_8K = "pcm_8k"
    PCM_16K = "pcm_16k"

    @property
    def sample_rate(self) -> int:
        return 8000 if self is AudioFormat.PCM_8K else 16000


class Session(BaseModel):
    """세션 레지스트리 레코드. CORE-BUS가 소유한다."""

    session_id: str
    tenant_id: str
    profile: SessionProfile
    audio_format: AudioFormat = AudioFormat.PCM_16K
    state: SessionState = SessionState.ACTIVE
    language: str = "ko"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class SessionCreate(BaseModel):
    """POST /internal/v1/sessions 요청 본문."""

    tenant_id: str
    profile: SessionProfile
    audio_format: AudioFormat = AudioFormat.PCM_16K
    language: str = "ko"
    metadata: dict[str, str] = Field(default_factory=dict)
