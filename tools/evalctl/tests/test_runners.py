"""실행기 검증 — 채점이 실제로 옳은지.

실행기가 틀리면 모든 수치가 틀린다. 그런데 그 사실은 아무도 눈치채지 못한다.
"""

from __future__ import annotations

from pathlib import Path

from evalctl.dataset import Article, PiiCase, RetrievalCase, TtsCase, load_corpus
from evalctl.runners import run_pii, run_retrieval, run_tts

REPO_EVAL = Path(__file__).resolve().parents[3] / "eval"


def _articles() -> list[Article]:
    return load_corpus(REPO_EVAL / "corpus" / "card-terms.md")


async def test_retrieval_folds_chunks_into_articles() -> None:
    cases = [RetrievalCase(case_id="fee", query="재발급 수수료", expected=["제3조"])]
    outcomes = await run_retrieval(cases, _articles())
    assert len(outcomes) == 1
    assert outcomes[0].returned == list(dict.fromkeys(outcomes[0].returned))
    assert outcomes[0].rank == 1
    assert outcomes[0].latency_ms > 0


async def test_retrieval_marks_miss_as_no_rank() -> None:
    cases = [RetrievalCase(case_id="nonsense", query="zzz qqq", expected=["제99조"])]
    outcomes = await run_retrieval(cases, _articles())
    assert outcomes[0].rank is None
    assert outcomes[0].hit is False


async def test_retrieval_respects_top_k() -> None:
    cases = [RetrievalCase(case_id="fee", query="수수료", expected=["제3조"])]
    outcomes = await run_retrieval(cases, _articles(), top_k=1)
    assert len(outcomes[0].returned) <= 1


def test_pii_counts_over_masking_as_failure() -> None:
    """과탐도 실패다. 금액을 지우면 상담 자체가 불가능해진다."""
    outcomes = run_pii(
        [PiiCase(case_id="amount", text="결제 금액은 1,234,567원입니다", expect_types=[])]
    )
    assert outcomes[0].passed


def test_pii_detects_leftover_original() -> None:
    """유형은 맞게 잡았는데 원문이 남아 있는 경우까지 잡아낸다."""
    outcomes = run_pii(
        [
            PiiCase(
                case_id="leak",
                text="주민번호 900101-1234567",
                expect_types=["rrn"],
                must_not_contain=["900101-1234567"],
            )
        ]
    )
    assert outcomes[0].passed

    contrived = run_pii(
        [PiiCase(case_id="x", text="카드 뒷자리 3456", expect_types=[], must_not_contain=["3456"])]
    )
    assert not contrived[0].passed
    assert "원문 잔존" in contrived[0].detail


def test_pii_reports_missed_and_extra_separately() -> None:
    outcomes = run_pii([PiiCase(case_id="x", text="안녕하세요", expect_types=["rrn"])])
    assert not outcomes[0].passed
    assert "미탐" in outcomes[0].detail

    over = run_pii([PiiCase(case_id="y", text="연락처 010-1234-5678", expect_types=[])])
    assert not over[0].passed
    assert "과탐" in over[0].detail


def test_tts_requires_exact_match() -> None:
    assert run_tts([TtsCase(case_id="a", text="3개", expect="세 개")])[0].passed
    wrong = run_tts([TtsCase(case_id="b", text="3개", expect="삼 개")])[0]
    assert not wrong.passed
    assert "기대" in wrong.detail
