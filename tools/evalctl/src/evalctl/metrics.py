"""품질 지표.

**Recall@K만 보면 회귀를 놓친다.** 정답 문서가 1위에서 3위로 밀려도 Recall@3은
1.0 그대로다. 실제로는 상담원이 세 번째 카드까지 눈을 내려야 한다는 뜻이고,
1초 예산 안에서 그 차이는 크다. 그래서 **MRR**을 함께 본다 — 순위가 내려가면
바로 떨어진다.

nDCG는 정답이 여러 개이고 서로 중요도가 다를 때 쓴다. 약관 검색은 대개
"이 조항 하나"가 정답이라 MRR로 충분하지만, FAQ처럼 여러 문서가 부분적으로
맞는 경우를 위해 함께 계산한다.

지연은 평균이 아니라 **p95**를 본다. 평균은 느린 소수를 숨기고, 상담원이
체감하는 것은 그 소수다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass
class CaseOutcome:
    """한 건의 채점 결과."""

    case_id: str
    query: str
    rank: int | None
    """정답이 나온 순위(1부터). 못 찾으면 ``None``."""

    returned: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    note: str = ""

    @property
    def hit(self) -> bool:
        return self.rank is not None

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.rank if self.rank else 0.0


@dataclass
class Metrics:
    """평가 한 판의 결과. 이 숫자들이 베이스라인과 비교된다."""

    total: int = 0
    hits: int = 0
    recall_at_k: float = 0.0
    mrr: float = 0.0
    """평균 역순위. **순위가 내려가면 떨어진다** — Recall이 못 보는 회귀를 잡는다."""

    ndcg_at_k: float = 0.0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    top_k: int = 3

    def to_dict(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "hits": self.hits,
            "recall_at_k": self.recall_at_k,
            "mrr": self.mrr,
            "ndcg_at_k": self.ndcg_at_k,
            "mean_latency_ms": self.mean_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "top_k": self.top_k,
        }


@dataclass
class CheckOutcome:
    """정답이 하나로 정해진 사례의 채점 결과 (PII 마스킹, TTS 읽기).

    검색과 달리 순위 개념이 없다. 맞거나 틀리거나다.
    """

    case_id: str
    passed: bool
    detail: str = ""
    """틀렸을 때 무엇이 어긋났는지. 리포트에서 이 줄만 보고 고칠 수 있어야 한다."""

    known_limitation: bool = False
    latency_ms: float = 0.0

    severity: str = ""
    """실패의 종류. 모든 실패가 같은 무게는 아니다.

    의도 라우팅에서 ``abstain``(되묻기)은 고객이 다시 말하면 회복되지만
    ``misroute``(오라우팅)는 엉뚱한 안내를 끝까지 듣고 나서야 드러난다.
    정확도 한 숫자에 섞으면 그 차이가 사라진다."""


@dataclass
class CheckMetrics:
    total: int = 0
    passed: int = 0
    accuracy: float = 0.0
    known_limitations: int = 0
    """'현재 못 한다'를 고정한 사례 수. 통과 건수에 섞이면 성적이 부풀려진다."""

    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "passed": self.passed,
            "accuracy": self.accuracy,
            "known_limitations": self.known_limitations,
            "mean_latency_ms": self.mean_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
        }


def summarize_checks(outcomes: list[CheckOutcome]) -> CheckMetrics:
    if not outcomes:
        return CheckMetrics()
    latencies = [o.latency_ms for o in outcomes]
    passed = sum(1 for o in outcomes if o.passed)
    return CheckMetrics(
        total=len(outcomes),
        passed=passed,
        accuracy=round(passed / len(outcomes), 4),
        known_limitations=sum(1 for o in outcomes if o.known_limitation),
        mean_latency_ms=round(statistics.fmean(latencies), 3),
        p95_latency_ms=percentile(latencies, 0.95),
    )


def rank_of(returned: list[str], expected: list[str]) -> int | None:
    """정답이 처음 나온 순위(1부터). 없으면 ``None``."""
    wanted = set(expected)
    return next((i + 1 for i, doc_id in enumerate(returned) if doc_id in wanted), None)


def ndcg(returned: list[str], expected: list[str], top_k: int) -> float:
    """정답이 여럿일 때의 순위 품질. 이상적 배치 대비 비율이다."""
    wanted = set(expected)
    if not wanted:
        return 0.0
    gain = sum(
        1.0 / math.log2(i + 2) for i, doc_id in enumerate(returned[:top_k]) if doc_id in wanted
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(wanted), top_k)))
    return gain / ideal if ideal else 0.0


def percentile(values: list[float], fraction: float) -> float:
    """분위수. 표본이 적으면 마지막 값에 수렴한다 — 그 사실을 리포트가 알린다."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 3)


def summarize(outcomes: list[CaseOutcome], top_k: int = 3) -> Metrics:
    if not outcomes:
        return Metrics(top_k=top_k)

    latencies = [o.latency_ms for o in outcomes]
    hits = sum(1 for o in outcomes if o.hit)
    return Metrics(
        total=len(outcomes),
        hits=hits,
        recall_at_k=round(hits / len(outcomes), 4),
        mrr=round(statistics.fmean(o.reciprocal_rank for o in outcomes), 4),
        ndcg_at_k=round(statistics.fmean(ndcg(o.returned, o.expected, top_k) for o in outcomes), 4),
        mean_latency_ms=round(statistics.fmean(latencies), 2),
        p95_latency_ms=percentile(latencies, 0.95),
        top_k=top_k,
    )
