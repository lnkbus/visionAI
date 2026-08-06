"""지어낸 말을 버리되, **진짜 말을 버리지 않는가.**

맥 데모에서 아무도 말하지 않았는데 이런 자막이 떴다:

    고객  감사합니다.
    고객  네, 네, 네, 네, 네.
    고객  달려!

Whisper 는 유튜브 자막으로 학습됐고, 무음·잡음 구간을 받으면 그 상용구를
뱉는다. 고장이 아니라 원래 그렇다.

이 시험의 무게중심은 **버리는 쪽이 아니라 남기는 쪽**이다. 문구만 보고
버리는 필터를 만들면 상담에서 고객이 가장 많이 하는 말인 "감사합니다"가
녹취에서 사라진다. 그리고 **아무도 눈치채지 못한다** — 화면에 안 나온 말은
없었던 말처럼 보이기 때문이다. 잡음 몇 줄이 남는 쪽이 훨씬 덜 나쁘다.
"""

from __future__ import annotations

import pytest

from vai_stt_core.hallucination import Evidence, HallucinationFilter, build_filter, normalize

# 마이크에 대고 또렷이 말했을 때의 근거. 실제 값은 이보다 좋을 때가 많다.
CLEAR = {"no_speech_prob": 0.01, "avg_logprob": -0.2}
# 무음 구간을 받았을 때 엔진이 스스로 의심하는 모습.
SILENCE = {"no_speech_prob": 0.85, "avg_logprob": -0.9}
# 애매한 구간. 일반 기준은 통과하지만 상용구 기준에는 걸린다 —
# 상용구 대조가 실제로 도는 유일한 구간이라 여기서 시험해야 한다.
MILD = {"no_speech_prob": 0.4, "avg_logprob": -0.4}


@pytest.fixture
def guard() -> HallucinationFilter:
    return HallucinationFilter()


def test_무음에서_나온_상용구를_버린다(guard: HallucinationFilter) -> None:
    verdict = guard.judge(Evidence("감사합니다.", **SILENCE))

    assert not verdict.keep
    assert verdict.reason, "왜 버렸는지 없으면 나중에 원인을 못 찾는다"


def test_또렷이_말한_감사합니다는_남긴다(guard: HallucinationFilter) -> None:
    """이 시험이 이 파일에서 가장 중요하다.

    상용구 목록에 있다는 이유만으로 버리면, 상담 녹취에서 고객이 가장 많이
    하는 말이 통째로 사라진다. 그리고 사라진 것은 아무도 못 본다."""
    verdict = guard.judge(Evidence("감사합니다.", **CLEAR))

    assert verdict.keep, f"진짜 발화를 버렸다: {verdict.reason}"


@pytest.mark.parametrize(
    "text",
    [
        "네 정말 감사합니다",
        "감사합니다 그럼 다음에 또 연락드릴게요",
        "이용해 주셔서 감사합니다 고객님",
    ],
)
def test_상용구를_품은_문장은_건드리지_않는다(guard: HallucinationFilter, text: str) -> None:
    """대조는 문장 전체가 정확히 그 상용구일 때만이다. 부분 일치로 하면
    멀쩡한 상담 문장이 통째로 날아간다."""
    assert guard.judge(Evidence(text, **MILD)).keep, f"진짜 문장을 버렸다: {text}"


def test_같은_말_반복은_버린다(guard: HallucinationFilter) -> None:
    """반복 루프는 발화가 아니라 디코더 상태다. 근거가 좋아도 버린다 —
    실제로 화면에 뜬 "네, 네, 네, 네, 네." 가 그것이었다."""
    verdict = guard.judge(Evidence("네, 네, 네, 네, 네.", **CLEAR))

    assert not verdict.keep
    assert "반복" in verdict.reason


def test_맞장구_두세_번은_남긴다(guard: HallucinationFilter) -> None:
    """상담에서 "네, 네" 는 정상 발화다. 반복 판정을 너무 조이면 이것이 죽는다."""
    assert guard.judge(Evidence("네, 네.", **CLEAR)).keep
    assert guard.judge(Evidence("네 네 네", **CLEAR)).keep


def test_서로_다른_낱말이면_반복이_아니다(guard: HallucinationFilter) -> None:
    assert guard.judge(Evidence("네 알겠습니다 네 알겠습니다", **CLEAR)).keep


def test_무음_확률이_높으면_문구와_무관하게_버린다(guard: HallucinationFilter) -> None:
    """"달려!" 처럼 목록에 없는 말도 지어낸다. 목록으로는 절대 다 못 막는다 —
    근거로 막아야 한다."""
    verdict = guard.judge(Evidence("달려!", no_speech_prob=0.9, avg_logprob=-0.3))

    assert not verdict.keep
    assert "무음" in verdict.reason


def test_근거가_약하면_버린다(guard: HallucinationFilter) -> None:
    verdict = guard.judge(Evidence("어쩌구 저쩌구", no_speech_prob=0.1, avg_logprob=-1.6))

    assert not verdict.keep
    assert "근거" in verdict.reason


def test_빈_결과는_버린다(guard: HallucinationFilter) -> None:
    assert not guard.judge(Evidence("   ", **CLEAR)).keep


def test_정상_상담_발화는_전부_통과한다(guard: HallucinationFilter) -> None:
    """필터를 넣고 나면 정작 아무것도 안 남는 사고가 이 종류에서 제일 흔하다."""
    utterances = [
        "안녕하세요 무배당 행복플러스 보험 문의드리려고요",
        "지난달에 청구한 실손 보험금이 아직 안 들어왔는데요",
        "네 그러면 다시 확인해 보고 연락드리겠습니다",
        "제 주민등록번호는 말씀드리기 좀 그런데요",
        "환불 절차가 어떻게 되나요",
    ]

    for text in utterances:
        assert guard.judge(Evidence(text, **CLEAR)).keep, f"정상 발화를 버렸다: {text}"


def test_현장에서_임계값을_조일_수_있다() -> None:
    """소음이 심한 콜센터와 조용한 회의실은 기준이 다르다. 코드를 고쳐야만
    조정되는 값이면 현장에서는 못 고친다."""
    strict = build_filter({"no_speech_threshold": 0.1})

    assert not strict.judge(Evidence("감사합니다", no_speech_prob=0.2, avg_logprob=-0.1)).keep
    assert HallucinationFilter().judge(
        Evidence("감사합니다", no_speech_prob=0.2, avg_logprob=-0.1)
    ).keep


def test_현장_상용구를_더할_수_있다() -> None:
    """고객사 안내 멘트가 무음 구간에 반복해 뜨는 경우가 있다."""
    guard = build_filter({"boilerplate_extra": ["상담원을 연결해 드리겠습니다"]})

    assert not guard.judge(Evidence("상담원을 연결해 드리겠습니다.", **MILD)).keep
    assert guard.judge(Evidence("상담원을 연결해 드리겠습니다.", **CLEAR)).keep


def test_대조는_공백과_문장부호를_무시한다() -> None:
    assert normalize("감사합니다.") == normalize(" 감사 합니다 ! ")
