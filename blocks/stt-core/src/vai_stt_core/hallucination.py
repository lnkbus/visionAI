"""Whisper 가 없는 말을 지어냈는지 가린다.

Whisper 는 유튜브 자막으로 학습됐다. 그래서 **무음이나 잡음 구간을 받으면
그 자막의 상용구를 뱉는다.** 맥 데모에서 나온 것이 정확히 그것이다:

    고객  감사합니다.
    고객  네, 네, 네, 네, 네.
    고객  달려!

아무도 그렇게 말하지 않았다. 에너지 VAD 가 노트북 마이크의 방 소음을 발화로
넘겼고, Whisper 는 넘어온 잡음에 대해 "가장 그럴듯한 자막"을 만들어 냈다.
엔진이 고장 난 것이 아니라 **원래 그렇게 동작한다.** 걸러 내는 것은 우리 몫이다.

여기서 가장 중요한 판단이 하나 있다. **"감사합니다" 를 무조건 버리면 안 된다.**
상담에서 고객이 실제로 가장 많이 하는 말 중 하나다. 문구만 보고 버리는 필터는
잡음을 잡는 대신 진짜 발화를 지운다 — 그쪽이 훨씬 나쁘다. 상담 녹취에서
"감사합니다"가 사라지면 아무도 그 사실을 눈치채지 못한다.

그래서 **문구가 아니라 근거로 가른다.**

* 어떤 문구든, 무음일 확률이 높거나 디코딩 근거가 약하면 버린다.
* 같은 말만 반복되면 버린다 (반복 루프는 발화가 아니라 디코더 상태다).
* 상용구는 **근거가 조금이라도 약할 때만** 버린다. 마이크에 대고 또렷이 말한
  "감사합니다"는 근거가 강하므로 남는다.

임계값은 전부 설정으로 뺀다. 여기 적힌 기본값은 출발점이지 측정된 진리가
아니다 — 현장 소음과 마이크에 따라 다르므로 평가셋으로 조정한다(docs/06 §5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 무음·잡음에서 자주 튀어나오는 자막 상용구. **정확히 이 말만일 때** 검사
# 대상이 된다("네 정말 감사합니다"는 여기에 안 걸린다).
BOILERPLATE = frozenset(
    {
        # 한국어 — 유튜브 자막 말미의 상투구
        "감사합니다",
        "고맙습니다",
        "시청해주셔서감사합니다",
        "오늘도시청해주셔서감사합니다",
        "구독과좋아요부탁드립니다",
        "구독좋아요알림설정",
        "다음영상에서만나요",
        "다음시간에만나요",
        "안녕히계세요",
        "이상입니다",
        # 영어 — 같은 이유로 섞여 나온다
        "thankyou",
        "thanksforwatching",
        "pleasesubscribe",
        "bye",
        "you",
    }
)

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """공백·문장부호를 지우고 소문자로. 대조용이지 표시용이 아니다."""
    return _STRIP.sub("", text).lower()


@dataclass(frozen=True)
class Evidence:
    """한 구간에 대해 엔진이 준 판단 근거.

    Whisper 는 확률을 직접 주지 않으므로 이 둘이 우리가 가진 전부다.
    """

    text: str
    no_speech_prob: float = 0.0
    """이 구간이 무음일 확률. 엔진이 스스로 의심한 정도다."""

    avg_logprob: float = 0.0
    """토큰 평균 로그확률. 낮을수록 디코더가 확신 없이 찍었다는 뜻이다."""


@dataclass(frozen=True)
class Verdict:
    keep: bool
    reason: str = ""
    """버린 이유. **반드시 로그에 남긴다** — 인식이 안 된다는 신고가 들어왔을 때
    "안 들렸다"와 "걸러 냈다"를 구분할 방법이 이것뿐이다."""


@dataclass
class HallucinationFilter:
    no_speech_threshold: float = 0.6
    """이보다 무음 확률이 높으면 문구와 무관하게 버린다."""

    log_prob_threshold: float = -1.0
    """이보다 근거가 약하면 문구와 무관하게 버린다."""

    boilerplate_no_speech: float = 0.2
    boilerplate_log_prob: float = -0.5
    """상용구에만 적용하는 느슨한 기준. 진짜로 또렷이 말한 "감사합니다"는
    이 둘을 모두 통과하므로 남는다."""

    repeat_limit: int = 4
    """같은 낱말만 이만큼 이어지면 반복 루프로 본다.

    내리면 맞장구를 지운다 — "네, 네, 네"는 상담에서 실제로 나온다.
    올리면 "네, 네, 네, 네, 네."가 그대로 화면에 남는다.
    """

    boilerplate: frozenset[str] = field(default_factory=lambda: BOILERPLATE)

    def judge(self, evidence: Evidence) -> Verdict:
        text = evidence.text.strip()
        if not text:
            return Verdict(False, "빈 결과")

        if evidence.no_speech_prob > self.no_speech_threshold:
            return Verdict(False, f"무음일 확률이 높다 ({evidence.no_speech_prob:.2f})")

        if evidence.avg_logprob < self.log_prob_threshold:
            return Verdict(False, f"디코딩 근거가 약하다 ({evidence.avg_logprob:.2f})")

        words = _WORD.findall(text)
        if len(words) >= self.repeat_limit and len({w.lower() for w in words}) == 1:
            return Verdict(False, f"같은 말이 {len(words)}번 반복된다")

        if normalize(text) in self.boilerplate and (
            evidence.no_speech_prob > self.boilerplate_no_speech
            or evidence.avg_logprob < self.boilerplate_log_prob
        ):
            # 문구만으로는 절대 안 버린다. 근거가 함께 약할 때만이다.
            detail = f"무음 {evidence.no_speech_prob:.2f} · 근거 {evidence.avg_logprob:.2f}"
            return Verdict(False, f"무음 상용구 ({detail})")

        return Verdict(True)


def build_filter(config: dict[str, object]) -> HallucinationFilter:
    """어댑터 설정에서 필터를 만든다.

    ``VAI_STT_ADAPTER_CONFIG`` 로 현장에서 조정한다. 값을 안 주면 위 기본값이다.
    """

    def number(key: str, fallback: float) -> float:
        raw = config.get(key)
        return fallback if raw is None or raw == "" else float(str(raw))

    extra = config.get("boilerplate_extra") or []
    listed = list(extra) if isinstance(extra, list | tuple | set | frozenset) else []
    words = BOILERPLATE | {normalize(str(item)) for item in listed if str(item).strip()}

    return HallucinationFilter(
        no_speech_threshold=number("no_speech_threshold", 0.6),
        log_prob_threshold=number("log_prob_threshold", -1.0),
        boilerplate_no_speech=number("boilerplate_no_speech", 0.2),
        boilerplate_log_prob=number("boilerplate_log_prob", -0.5),
        repeat_limit=int(number("repeat_limit", 4)),
        boilerplate=frozenset(words),
    )
