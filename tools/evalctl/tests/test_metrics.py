"""지표 계산 검증.

여기서 지키려는 것 하나: **MRR이 순위 하락에 실제로 반응하는가.**
그게 안 되면 이 하네스를 만든 이유가 없다.
"""

from __future__ import annotations

from evalctl.metrics import (
    CaseOutcome,
    CheckOutcome,
    ndcg,
    percentile,
    rank_of,
    summarize,
    summarize_checks,
)


def _outcome(case_id: str, returned: list[str], expected: list[str]) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        query="q",
        rank=rank_of(returned, expected),
        returned=returned,
        expected=expected,
        latency_ms=1.0,
    )


def test_recall_blind_to_rank_but_mrr_is_not() -> None:
    """정답이 1위에서 3위로 밀린 상황. Recall@3은 그대로고 MRR만 떨어진다."""
    before = [_outcome("a", ["제3조", "제4조", "제5조"], ["제3조"])]
    after = [_outcome("a", ["제4조", "제5조", "제3조"], ["제3조"])]

    assert summarize(before).recall_at_k == summarize(after).recall_at_k == 1.0
    assert summarize(before).mrr == 1.0
    assert summarize(after).mrr < summarize(before).mrr


def test_rank_of_finds_first_hit() -> None:
    assert rank_of(["a", "b", "c"], ["c", "b"]) == 2
    assert rank_of(["a", "b"], ["z"]) is None
    assert rank_of([], ["a"]) is None


def test_ndcg_rewards_putting_both_answers_high() -> None:
    both_top = ndcg(["제4조", "제13조", "제9조"], ["제4조", "제13조"], 3)
    one_top = ndcg(["제4조", "제9조", "제13조"], ["제4조", "제13조"], 3)
    assert both_top == 1.0
    assert one_top < both_top


def test_ndcg_without_expected_is_zero() -> None:
    assert ndcg(["a"], [], 3) == 0.0


def test_percentile_converges_to_last_value_on_small_samples() -> None:
    # 표본이 적으면 p95는 최댓값이 된다. 이 사실을 모르면 수치를 과신하게 된다.
    assert percentile([1.0, 2.0, 3.0], 0.95) == 3.0
    assert percentile([], 0.95) == 0.0


def test_summarize_empty_keeps_top_k() -> None:
    metrics = summarize([], top_k=5)
    assert metrics.total == 0
    assert metrics.top_k == 5


def test_known_limitations_counted_separately() -> None:
    outcomes = [
        CheckOutcome(case_id="a", passed=True),
        CheckOutcome(case_id="b", passed=True, known_limitation=True),
        CheckOutcome(case_id="c", passed=False, detail="미탐"),
    ]
    metrics = summarize_checks(outcomes)
    assert metrics.passed == 2
    assert metrics.known_limitations == 1
    # 통과 건수에 한계 사례가 섞여 있다는 사실이 리포트에 드러나야 한다.
    assert metrics.accuracy == round(2 / 3, 4)
