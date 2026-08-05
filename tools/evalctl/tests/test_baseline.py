"""회귀 판정 규칙 검증.

게이트가 지켜야 할 두 가지를 각각 확인한다: 진짜 회귀를 잡을 것,
그리고 무관한 변동으로 사람을 지치게 하지 말 것.
"""

from __future__ import annotations

from pathlib import Path

from evalctl.baseline import blocking, compare, load_baseline, save_baseline

BASE = {
    "retrieval": {"total": 34.0, "recall_at_k": 0.94, "mrr": 0.86, "mean_latency_ms": 0.6},
    "pii": {"total": 20.0, "accuracy": 1.0, "known_limitations": 1.0},
}


def test_rank_drop_is_caught_even_when_recall_holds() -> None:
    current = {
        "retrieval": {"total": 34.0, "recall_at_k": 0.94, "mrr": 0.79, "mean_latency_ms": 0.6},
        "pii": BASE["pii"],
    }
    found = blocking(compare(current, BASE))
    assert [f.metric for f in found] == ["mrr"]
    assert found[0].delta < 0


def test_deleting_failing_cases_is_a_regression() -> None:
    """실패 사례를 지워 초록을 만드는 경로를 막는다."""
    current = {
        "retrieval": {"total": 30.0, "recall_at_k": 1.0, "mrr": 1.0},
        "pii": BASE["pii"],
    }
    found = blocking(compare(current, BASE))
    assert [f.metric for f in found] == ["total"]


def test_dropping_a_whole_suite_is_a_regression() -> None:
    found = blocking(compare({"retrieval": BASE["retrieval"]}, BASE))
    assert [(f.suite, f.metric) for f in found] == [("pii", "__suite__")]


def test_latency_never_blocks() -> None:
    current = {
        "retrieval": {**BASE["retrieval"], "mean_latency_ms": 60.0},
        "pii": BASE["pii"],
    }
    findings = compare(current, BASE)
    assert not blocking(findings)
    # 실패시키지는 않되 조용하지도 않다.
    assert any(f.metric == "mean_latency_ms" for f in findings)


def test_sub_millisecond_jitter_stays_silent() -> None:
    """0.6ms → 1.5ms는 러너 사정이지 회귀가 아니다. 매번 뜨는 경고는 소음이다."""
    current = {
        "retrieval": {**BASE["retrieval"], "mean_latency_ms": 1.5},
        "pii": BASE["pii"],
    }
    assert not compare(current, BASE)


def test_new_known_limitation_warns_only() -> None:
    current = {
        "retrieval": BASE["retrieval"],
        "pii": {**BASE["pii"], "known_limitations": 2.0},
    }
    findings = compare(current, BASE)
    assert not blocking(findings)
    assert any(f.metric == "known_limitations" for f in findings)


def test_improvement_is_clean() -> None:
    current = {
        "retrieval": {**BASE["retrieval"], "mrr": 0.92, "total": 40.0},
        "pii": BASE["pii"],
    }
    assert not compare(current, BASE)


def test_new_suite_warns_but_does_not_block() -> None:
    current = {**BASE, "tts": {"total": 18.0, "accuracy": 1.0}}
    findings = compare(current, BASE)
    assert not blocking(findings)
    assert [f.suite for f in findings] == ["tts"]


def test_baseline_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "baseline.json"
    save_baseline(path, BASE, note="최초 고정")
    assert load_baseline(path) == BASE


def test_missing_baseline_reads_as_empty(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "nope.json") == {}


def test_baseline_file_is_stable_across_identical_runs(tmp_path: Path) -> None:
    """같은 수치면 파일도 같아야 diff가 '무엇이 달라졌는가'를 말해 준다."""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    save_baseline(first, BASE, note="x")
    save_baseline(second, BASE, note="x")
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
