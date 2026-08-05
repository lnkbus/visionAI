"""WER/CER 이 실제로 무엇을 재는가."""

from __future__ import annotations

import pytest

from evalctl.asr import AsrOutcome, normalize, score, summarize_asr


def test_perfect_match_is_zero() -> None:
    out = score("c1", "카드 재발급 신청합니다", "카드 재발급 신청합니다")
    assert out.word_errors == 0 and out.char_errors == 0


@pytest.mark.parametrize(
    ("ref", "hyp"),
    [
        ("카드 재발급 신청합니다.", "카드 재발급 신청합니다"),
        ("카드  재발급   신청합니다", "카드 재발급 신청합니다"),
        ("Card 재발급", "card 재발급"),
    ],
    ids=["구두점", "중복공백", "대소문자"],
)
def test_표기_차이는_오류가_아니다(ref: str, hyp: str) -> None:
    """표기 차이를 오류로 세면 모델을 바꿔도 숫자가 안 움직인다."""
    out = score("c", ref, hyp)
    assert out.word_errors == 0 and out.char_errors == 0


def test_nfd_정답이_통째로_어긋나지_않는다() -> None:
    """macOS 에서 만든 파일은 자모가 분리돼 온다. 눈에는 같은데 글자 수가 다르다."""
    import unicodedata

    nfc = "회의록"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc, "이 환경에서 NFD 분해가 일어나지 않았다 — 시험이 성립하지 않는다"
    assert len(nfd) > len(nfc), "분해되면 글자 수가 늘어난다 — 그게 CER을 어긋나게 한다"
    assert normalize(nfd) == normalize(nfc)
    assert score("c", nfd, nfc).char_errors == 0


def test_띄어쓰기만_다르면_cer은_관대하고_wer은_가혹하다() -> None:
    """한국어에서 기준선을 CER로 잡는 이유가 이것이다."""
    out = score("c", "회의록 정리", "회의 록 정리")
    assert out.char_errors == 0, "글자는 같다"
    assert out.word_errors > 0, "어절은 갈렸다"


def test_빈_인식은_따로_센다() -> None:
    """오류율만 보면 '아무것도 못 냄'과 '전부 틀림'이 구분되지 않는다."""
    out = score("c", "카드 재발급", "")
    assert out.empty
    assert out.char_errors == len("카드재발급")


def test_코퍼스_비율은_길이로_가중된다() -> None:
    """사례별 비율을 평균 내면 짧은 발화가 과대 대표된다.

    "네" 한 글자를 틀리면 그 사례의 CER은 100%다. 그것을 긴 발화와 같은
    무게로 평균 내면 코퍼스 품질이 아니라 짧은 발화 비율을 재게 된다.
    """
    short = score("s", "네", "예")  # 1글자 중 1 오류
    long = score("l", "카드 재발급 신청합니다", "카드 재발급 신청합니다")  # 11글자 중 0

    metrics = summarize_asr([short, long])
    naive_mean = (1.0 + 0.0) / 2

    assert metrics.cer < naive_mean, "길이 가중이 안 되고 있다"
    assert metrics.cer == pytest.approx(1 / 12, abs=0.01)


def test_빈_입력은_0으로_떨어진다() -> None:
    assert summarize_asr([]).total == 0


def test_p95_지연을_함께_낸다() -> None:
    outs = [
        AsrOutcome("c", "가", "가", 0, 1, 0, 1, latency_ms=float(ms))
        for ms in (10, 20, 30, 40, 1000)
    ]
    assert summarize_asr(outs).p95_latency_ms >= 40


def test_metrics_는_직렬화된다() -> None:
    got = summarize_asr([score("c", "가나", "가다")]).to_dict()
    assert set(got) == {"total", "wer", "cer", "empty", "p95_latency_ms"}
    assert got["cer"] == 0.5
