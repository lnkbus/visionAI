"""품질 회귀 게이트.

평가 하네스를 별도 명령으로만 두면 아무도 돌리지 않는다. 골든셋 전체가
결정적이고 1초 안에 끝나므로 **일반 테스트와 같이 매번 돈다**.

여기서 실패했다는 것은 코드가 깨졌다는 뜻이 아니라 **품질이 내려갔다**는
뜻이다. 둘은 다른 사건이고, 대응도 다르다:

* 의도치 않은 하락이면 → 원인을 고친다.
* 의도한 트레이드오프이면 → ``evalctl baseline --note "..."``로 기준선을
  갱신하고, 그 note가 왜 내려도 되는지를 설명한다.

기준선을 조용히 갱신하는 것만은 하지 않는다. 그 순간부터 이 게이트는
아무것도 지키지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evalctl.baseline import blocking, compare, load_baseline
from evalctl.cli import SUITE_NAMES, run_suites

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "eval"


@pytest.fixture(scope="module")
def current() -> dict[str, dict[str, float]]:
    results = run_suites(EVAL_DIR, list(SUITE_NAMES), top_k=3)
    return {result.name: result.metrics for result in results}


def test_baseline_exists() -> None:
    assert load_baseline(EVAL_DIR / "baseline.json"), (
        "기준선이 없다 — `uv run evalctl baseline`으로 고정한다"
    )


def test_no_quality_regression(current: dict[str, dict[str, float]]) -> None:
    findings = blocking(compare(current, load_baseline(EVAL_DIR / "baseline.json")))
    detail = "\n".join(f"  [{f.suite}] {f.metric}: {f.message}" for f in findings)
    assert not findings, f"품질 회귀:\n{detail}"


def test_masking_and_reading_stay_perfect(current: dict[str, dict[str, float]]) -> None:
    """PII와 읽기는 100%가 기준이다.

    검색은 못 찾으면 상담원이 직접 찾으면 되지만, 마스킹 실패는 유출이고
    숫자 오독은 실패한 통화다. 이 둘에는 '대부분 맞음'이라는 상태가 없다.
    """
    assert current["pii"]["accuracy"] == 1.0
    assert current["tts"]["accuracy"] == 1.0
