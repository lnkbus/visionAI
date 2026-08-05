"""아바타 계약 — 입 모양(viseme) 타임라인과 표정.

**영상을 서버에서 만들어 보내지 않는다.** 세션마다 렌더링하면 GPU가 통화 수만큼
필요하고, 인코딩·전송 지연이 음성 위에 더해진다. 서버는 **입 모양 타임라인**만
보내고 그리는 것은 클라이언트(브라우저·키오스크)가 한다 — 수십 바이트로 끝난다.

**동기가 전부다.** 소리와 입 모양이 어긋나면 사람은 즉시 알아채고, 어긋난
아바타는 없느니만 못하다. 그래서 타임라인을 TTS 조각의 실제 길이에 맞춘다 —
합성 결과를 받은 뒤에 계산하므로 추정이 아니라 실측이다.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from vai_contracts.events import BaseEvent


class Viseme(StrEnum):
    """입 모양 분류. 음소보다 거칠지만 **눈에 보이는 차이**만 남긴 단위다.

    한국어에서 시각적으로 가장 중요한 것은 양순음(ㅁㅂㅍ)의 입술 다물기다.
    이것이 틀리면 아바타가 즉시 가짜로 보인다.
    """

    SIL = "sil"
    """무음. 입을 다문 기본 자세."""

    PP = "pp"
    """양순음 ㅁ·ㅂ·ㅃ·ㅍ — 입술을 다문다. 가장 눈에 띄는 모양."""

    FF = "ff"
    """순치음. 한국어에는 없지만 외래어(f/v)에서 쓴다."""

    TH = "th"
    DD = "dd"
    """치조음 ㄴ·ㄷ·ㅌ·ㄹ — 혀끝이 윗니 뒤."""

    KK = "kk"
    """연구개음 ㄱ·ㅋ — 입은 조금 열리고 혀 뒤가 올라간다."""

    CH = "ch"
    """치찰음 ㅅ·ㅈ·ㅊ — 이를 살짝 붙이고 좁게."""

    AA = "aa"
    """ㅏ·ㅑ — 크게 벌린 입."""

    E = "e"
    """ㅔ·ㅐ — 중간 벌림."""

    IH = "ih"
    """ㅣ·ㅡ — 옆으로 벌린 입."""

    OH = "oh"
    """ㅗ·ㅓ — 둥글게."""

    OU = "ou"
    """ㅜ·ㅠ — 오므린 입술."""


class AvatarState(StrEnum):
    """말하지 않을 때의 상태.

    **가만히 멈춰 있으면 안 된다.** 정지한 아바타는 화면이 얼어붙은 것처럼
    보이고, 고객은 통화가 끊겼다고 여긴다. 듣는 중·생각 중을 구분해 보여 준다.
    """

    IDLE = "idle"
    LISTENING = "listening"
    """고객이 말하는 중. 고개를 살짝 끄덕이는 정도의 반응이 필요하다."""

    THINKING = "thinking"
    """검색·추론 중. 이 상태가 없으면 침묵이 고장으로 읽힌다."""

    SPEAKING = "speaking"
    HANDOFF = "handoff"
    """사람 상담원에게 넘기는 중."""


class VisemeFrame(BaseModel):
    """한 시점의 입 모양."""

    at_ms: int
    """조각 시작 기준 오프셋. 절대 시각이 아니라 상대값이라 재생 지연에 영향받지 않는다."""

    viseme: Viseme
    weight: float = Field(default=1.0, ge=0.0, le=1.0)
    """세기. 클라이언트가 프레임 사이를 보간한다 — 단계적으로 끊어 그리면
    입이 딸깍거리는 것처럼 보인다."""


class AvatarTrack(BaseEvent):
    """``avatar.track`` — 오디오 조각 하나에 대응하는 입 모양 타임라인.

    ``tts.audio``와 **같은 turn_id·seq_in_turn**을 쓴다. 클라이언트가 둘을
    짝지어 재생하므로 식별자가 어긋나면 동기가 통째로 깨진다.
    """

    turn_id: str
    seq_in_turn: int
    duration_ms: int
    text: str = ""
    frames: list[VisemeFrame] = Field(default_factory=list)
    state: AvatarState = AvatarState.SPEAKING


class AvatarStatus(BaseEvent):
    """``avatar.status`` — 말하지 않을 때의 상태 전환."""

    state: AvatarState
    reason: str = ""
