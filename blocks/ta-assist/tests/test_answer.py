"""추천 답변 검증 테스트.

여기서 통과시킨 문장은 상담원이 고객에게 그대로 읽는다. 그래서 검증기의
실패 방향은 대칭이 아니다 — **환각을 통과시키는 쪽이 훨씬 나쁘다.**
"""

from __future__ import annotations

import pytest

from vai_contracts.retrieval import Chunk, ScoredChunk
from vai_ta_assist.answer import (
    AnswerComposer,
    Evidence,
    build_evidence,
    numbers_in,
    render_prompt,
    verify,
)

FEE = "재발급 수수료는 3,000원이며 발급까지 영업일 기준 3일이 소요된다."
WAIVER = "직전 1년간 이용실적이 200만원 이상인 경우 다음 연도 연회비를 면제한다."


def ev(*texts: str) -> list[Evidence]:
    return [Evidence(index=i + 1, doc_id=f"d{i}", title="", text=t) for i, t in enumerate(texts)]


def test_grounded_answer_passes() -> None:
    verdict = verify("재발급 수수료는 3,000원이고 영업일 기준 3일이 걸립니다. [1]", ev(FEE))
    assert verdict.accepted
    assert verdict.citations == [1]


def test_invented_number_is_rejected() -> None:
    verdict = verify("재발급 수수료는 5,000원입니다. [1]", ev(FEE))
    assert not verdict.accepted
    assert "5000" in verdict.reason


def test_thousand_separator_is_not_a_different_number() -> None:
    """표기 차이로 멀쩡한 답변을 버리면 생성 기능 자체가 무용해진다."""
    assert verify("수수료는 3000원입니다. [1]", ev(FEE)).accepted


def test_decimal_point_is_significant() -> None:
    passage = "해외 이용 수수료는 이용금액의 1.0%이다."
    assert not verify("해외 수수료는 10%입니다. [1]", ev(passage)).accepted
    assert verify("해외 수수료는 1.0%입니다. [1]", ev(passage)).accepted


def test_answer_without_citation_is_rejected() -> None:
    verdict = verify("재발급 수수료는 3,000원입니다.", ev(FEE))
    assert "인용" in verdict.reason


def test_citation_out_of_range_is_rejected() -> None:
    verdict = verify("재발급 수수료는 3,000원입니다. [4]", ev(FEE))
    assert "존재하지 않는" in verdict.reason


def test_number_must_be_in_the_cited_passage_not_a_neighbour() -> None:
    """옆 근거에 있는 숫자로 다른 근거를 인용하면 상담원이 엉뚱한 조항을 읽는다."""
    verdict = verify("이용실적 200만원 이상이면 면제됩니다. [1]", ev(FEE, WAIVER))
    assert not verdict.accepted


def test_wholesale_fabrication_is_caught_even_when_numbers_match() -> None:
    """숫자만 검사하면 근거에 없는 문장을 통째로 지어내도 통과한다."""
    passage = "회원은 매월 1일, 5일, 10일, 15일, 20일, 25일 중에서 결제일을 지정할 수 있다."
    answer = (
        "결제일은 매월 1일, 5일, 10일, 15일, 20일, 25일 중에서 지정하실 수 있으며 "
        "변경을 원하시면 고객센터 또는 모바일 앱을 통해 신청하실 수 있고 "
        "자세한 안내는 홈페이지에서도 확인하실 수 있습니다. [1]"
    )
    assert not verify(answer, ev(passage)).accepted


def test_honorific_rewrite_still_passes() -> None:
    """존대 변환만으로 표면 토큰이 달라진다. 이걸 환각으로 보면 아무 답변도 못 쓴다."""
    assert verify("연회비는 면제하실 수 있습니다. [1]", ev(WAIVER)).accepted


def test_overpromise_is_rejected() -> None:
    verdict = verify("실적만 채우면 반드시 면제됩니다. [1]", ev(WAIVER))
    assert "확약" in verdict.reason


def test_long_answer_is_rejected() -> None:
    long_passage = " ".join([WAIVER] * 4)
    verdict = verify(" ".join([WAIVER] * 4) + " [1]", ev(long_passage))
    assert "길이" in verdict.reason


def test_model_refusal_is_not_an_answer() -> None:
    verdict = verify("답변불가", ev(FEE))
    assert not verdict.accepted
    assert "근거 부족" in verdict.reason


def test_numbers_in_normalizes_separators() -> None:
    assert numbers_in("3,000원과 3000원") == {"3000"}
    assert numbers_in("1.0%") == {"1.0"}
    assert numbers_in("숫자 없음") == set()


# ── 생성기 ───────────────────────────────────────────────────────────────────


def _hit(text: str) -> ScoredChunk:
    return ScoredChunk(
        chunk=Chunk(
            chunk_id="c1", doc_id="제3조", tenant_id="t", kb_id="kb", title="제3조", text=text
        ),
        score=1.0,
    )


async def test_no_evidence_means_no_generation() -> None:
    """근거가 없으면 만들지 않는다. 이 규칙에는 예외를 두지 않는다."""
    called = False

    async def complete(prompt: str, system: str = "") -> str:
        nonlocal called
        called = True
        return "무엇이든 답합니다. [1]"

    verdict = await AnswerComposer(complete).compose("질문", [])

    assert not verdict.accepted
    assert not called, "근거 없이 모델을 호출해서는 안 된다"


async def test_composer_without_llm_generates_nothing() -> None:
    verdict = await AnswerComposer().compose("질문", [_hit(FEE)])
    assert not verdict.accepted


async def test_generation_failure_falls_back_to_evidence_only() -> None:
    async def broken(prompt: str, system: str = "") -> str:
        raise RuntimeError("LLM 다운")

    verdict = await AnswerComposer(broken).compose("수수료 얼마예요", [_hit(FEE)])

    assert not verdict.accepted
    assert "실패" in verdict.reason


async def test_hallucinated_answer_never_reaches_the_agent() -> None:
    async def liar(prompt: str, system: str = "") -> str:
        return "재발급 수수료는 9,900원입니다. [1]"

    verdict = await AnswerComposer(liar).compose("수수료 얼마예요", [_hit(FEE)])

    assert not verdict.accepted


async def test_grounded_generation_is_accepted() -> None:
    async def honest(prompt: str, system: str = "") -> str:
        assert "[근거]" in prompt
        return "재발급 수수료는 3,000원입니다. [1]"

    verdict = await AnswerComposer(honest).compose("수수료 얼마예요", [_hit(FEE)])

    assert verdict.accepted
    assert verdict.citations == [1]


def test_prompt_numbers_the_evidence() -> None:
    prompt = render_prompt("수수료 얼마예요", build_evidence([_hit(FEE), _hit(WAIVER)]))
    assert "[1]" in prompt and "[2]" in prompt
    assert "수수료 얼마예요" in prompt


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_blank_responses_are_rejected(bad: str) -> None:
    assert not verify(bad, ev(FEE)).accepted
