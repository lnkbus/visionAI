"""음성 합성 계약 — 사양서 §3의 TTS 모듈.

**첫 소리까지의 지연(TTFB)이 이 영역의 전부다.** 사람은 상대가 0.5초 안에
반응하지 않으면 "안 들렸나?" 하고 다시 말한다. 전체 합성이 아무리 빨라도
첫 소리가 늦으면 대화가 무너지므로, 계약을 **문장 단위 스트리밍**으로 짠다.

**끼어들기(barge-in)는 선택 기능이 아니다.** 고객이 말을 시작했는데 봇이
계속 떠들면 그 통화는 실패한다. 취소가 이벤트로 표현되어야 재생 중인 오디오를
어느 지점에서든 끊을 수 있다.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from vai_contracts.events import BaseEvent

DEFAULT_SAMPLE_RATE = 24000
"""합성 기본 표본율. 전화(8kHz)로 나갈 때는 재표본화한다 — 합성을 8kHz로
직접 하면 음질이 눈에 띄게 나빠진다."""


class AudioEncoding(StrEnum):
    PCM16 = "pcm16"
    MULAW = "mulaw"
    """전화망(G.711). AUD-RTP가 그대로 실어 보낼 수 있는 형식이다."""


class VoiceStyle(StrEnum):
    NEUTRAL = "neutral"
    CALM = "calm"
    """상담 기본값. 밝은 톤은 민원·불만 응대에서 역효과가 난다."""

    BRIGHT = "bright"


class VoiceProfile(BaseModel):
    """목소리 설정. 고객사별로 다르므로 코드가 아니라 설정으로 다룬다."""

    voice_id: str = "default"
    language: str = "ko"
    style: VoiceStyle = VoiceStyle.CALM
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0)
    sample_rate: int = DEFAULT_SAMPLE_RATE
    encoding: AudioEncoding = AudioEncoding.PCM16


class SynthesisRequest(BaseEvent):
    """``tts.request`` — 합성 요청.

    ``turn_id``로 묶는다. 끼어들기가 들어오면 그 턴의 남은 조각을 전부 버려야
    하는데, 세션 단위로만 묶으면 다음 턴의 오디오까지 함께 버려진다.
    """

    turn_id: str
    text: str
    voice: VoiceProfile = Field(default_factory=VoiceProfile)
    priority: bool = False
    """True면 대기 중인 요청을 앞지른다. 시스템 안내(예: 녹취 고지)용."""


class SpeechChunk(BaseEvent):
    """``tts.audio`` — 합성된 오디오 조각.

    문장 단위로 나온다. 전체를 기다렸다 보내면 첫 소리가 문장 수만큼 늦어진다.
    """

    turn_id: str
    seq_in_turn: int
    pcm: bytes
    sample_rate: int
    encoding: AudioEncoding = AudioEncoding.PCM16
    text: str = ""
    """이 조각이 말하는 내용. 자막 표시와 디버깅에 쓴다."""

    is_final: bool = False
    duration_ms: int = 0


class SpeechCancel(BaseEvent):
    """``tts.cancel`` — 끼어들기.

    고객이 말을 시작했는데 봇이 계속 떠들면 그 통화는 실패한다.
    합성 중인 것과 재생 대기 중인 것을 **모두** 버려야 한다.
    """

    turn_id: str = ""
    """비면 세션의 모든 턴을 취소한다."""

    reason: str = "barge_in"
