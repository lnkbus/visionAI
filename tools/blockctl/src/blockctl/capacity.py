"""동시 채널 용량 산정 — "1,000석이면 장비가 몇 대인가".

제안·견적에서 가장 먼저 받는 질문이고, 지금까지 답이 없었다. 답이 없으면
영업이 감으로 말하게 되고, 그 숫자가 계약서에 들어간다.

**상담석 하나가 채널 하나가 아니다.** 상담사와 고객이 동시에 말하므로
녹취·인식은 **2채널**이 필요하다(``CHANNELS_PER_SEAT``). 1,000석이면
2,000채널이며, 이것을 1,000으로 잡으면 도입 직후 절반이 인식되지 않는다.
이 실수는 현장에서 "가끔 상담사 말이 안 잡힌다"로 나타나 원인을 찾기 어렵다.

동시성 계산에는 **동시 통화율**이 들어간다. 1,000석이 전부 동시에 통화하지는
않는다 — 대기·후처리·휴게가 있다. 다만 **피크는 평균이 아니다**: 콜센터의
아침 오픈과 점심 직후에 몰리므로, 평균 가동률로 사이징하면 그 시간대에 막힌다.
기본값을 보수적으로 두는 이유다.

여기 숫자들은 **설계 근거이지 실측이 아니다.** 성능 시험은 실서버가 필요하고
(docs/07 §3.2) 그때까지 이 값들은 가정으로 표시된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHANNELS_PER_SEAT = 2
"""상담석당 오디오 채널. 상담사 1 + 고객 1.

이것을 1로 잡는 것이 이 계산에서 가장 흔하고 가장 비싼 실수다."""

DEFAULT_CONCURRENCY = 0.75
"""동시 통화율 기본값. 피크 기준이라 평균 가동률보다 높게 잡는다."""


@dataclass(frozen=True)
class EngineProfile:
    """엔진 하나가 장비 하나에서 감당하는 양.

    **실측이 아니라 가정이다.** 실서버 성능 시험 전까지는 견적의 출발점일 뿐이며,
    보고서에도 그렇게 표시한다.
    """

    name: str
    channels_per_gpu: int
    """GPU 1장이 감당하는 동시 채널(가정)."""

    note: str = ""
    cpu_only: bool = False


# 값의 근거: Whisper 계열은 구간 단위 추론이라 GPU 하나에 여러 스트림을 태울 수
# 있고, 배치가 커질수록 처리량이 좋아진다. 반대로 지연은 나빠진다 — 실시간
# 자막에서는 배치를 키울 수 없어 여기 숫자가 보수적이다.
PROFILES: dict[str, EngineProfile] = {
    "stt-small-gpu": EngineProfile("STT small (GPU)", 64, "실시간 자막, 지연 우선"),
    "stt-large-gpu": EngineProfile("STT large-v3 (GPU)", 24, "정확도 우선"),
    "stt-small-cpu": EngineProfile(
        "STT small (CPU)", 4, "데모·소규모 전용. 상담센터 규모에는 쓰지 않는다", cpu_only=True
    ),
    "tts-gpu": EngineProfile("TTS (GPU)", 96, "문장 단위 스트리밍"),
    "spk-gpu": EngineProfile("화자분리 (GPU)", 128, "인식보다 가볍다. 배치로 뒤따라 처리"),
}


@dataclass(frozen=True)
class Requirement:
    engine: str
    channels: int
    gpus: int
    note: str = ""


@dataclass(frozen=True)
class Plan:
    seats: int
    concurrency: float
    channels: int
    """동시에 처리해야 하는 오디오 채널 수."""

    requirements: list[Requirement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "seats": self.seats,
            "concurrency": self.concurrency,
            "channels_per_seat": CHANNELS_PER_SEAT,
            "channels": self.channels,
            "requirements": [
                {"engine": r.engine, "channels": r.channels, "gpus": r.gpus, "note": r.note}
                for r in self.requirements
            ],
            "warnings": self.warnings,
        }


def plan_capacity(
    seats: int,
    *,
    concurrency: float = DEFAULT_CONCURRENCY,
    stt_profile: str = "stt-small-gpu",
    with_tts: bool = False,
    with_diarization: bool = True,
) -> Plan:
    """상담석 수에서 필요한 채널과 GPU를 계산한다."""
    if seats <= 0:
        raise ValueError("상담석 수는 1 이상이어야 한다")
    if not 0 < concurrency <= 1:
        raise ValueError("동시 통화율은 0 초과 1 이하다")
    if stt_profile not in PROFILES:
        raise ValueError(
            f"알 수 없는 프로파일: {stt_profile} (가능: {', '.join(sorted(PROFILES))})"
        )

    channels = _ceil(seats * CHANNELS_PER_SEAT * concurrency)
    warnings: list[str] = []
    requirements: list[Requirement] = []

    stt = PROFILES[stt_profile]
    requirements.append(
        Requirement("STT", channels, _ceil(channels / stt.channels_per_gpu), stt.note)
    )
    if stt.cpu_only and channels > PROFILES["stt-small-cpu"].channels_per_gpu * 4:
        warnings.append(
            f"CPU 추론으로 {channels}채널은 성립하지 않는다 — "
            "GPU 프로파일로 계산하거나 규모를 줄인다"
        )

    if with_diarization:
        spk = PROFILES["spk-gpu"]
        requirements.append(
            Requirement("화자분리", channels, _ceil(channels / spk.channels_per_gpu), spk.note)
        )
    if with_tts:
        tts = PROFILES["tts-gpu"]
        # 봇 응답은 전 채널이 동시에 말하지 않는다. 절반으로 잡되 그 가정을 적는다.
        tts_channels = _ceil(channels / 2)
        requirements.append(
            Requirement(
                "TTS",
                tts_channels,
                _ceil(tts_channels / tts.channels_per_gpu),
                "봇이 전 채널에서 동시에 말하지는 않는다 — 채널의 절반으로 가정",
            )
        )

    warnings.append("이 숫자는 **설계 가정**이다. 실서버 성능 시험 전까지 견적의 출발점으로만 쓴다")
    if seats >= 1000:
        warnings.append(
            "1,000석 이상은 단일 노드로 감당하지 않는다 — STT는 무상태라 수평 확장되지만, "
            "단일 라이터 블록(LLM-SUM)과 Redis는 별도 사이징이 필요하다"
        )

    return Plan(
        seats=seats,
        concurrency=concurrency,
        channels=channels,
        requirements=requirements,
        warnings=warnings,
    )


def _ceil(value: float) -> int:
    """올림. 0.1 GPU 라는 것은 없다 — 내림하면 피크에서 막힌다."""
    result = int(value)
    return result + 1 if value > result else max(result, 1)


def license_channels(seats: int, *, concurrency: float = DEFAULT_CONCURRENCY) -> int:
    """라이선스에 적을 동시 채널 수.

    라이선스는 **피크를 막는 것**이라 여유를 두지 않는다. 여유를 두면 초과
    사용이 청구되지 않고, 부족하면 피크에 429가 난다. 계산과 계약이 같은
    숫자를 쓰게 여기서 한 번에 낸다.
    """
    return plan_capacity(seats, concurrency=concurrency).channels
