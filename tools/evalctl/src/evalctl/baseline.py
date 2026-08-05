"""베이스라인 비교 — 회귀 게이트의 판정 규칙.

게이트가 신뢰를 잃는 방식은 둘뿐이다: **놓치거나(회귀가 통과), 시끄럽거나(무관한
변동이 빨강)**. 둘 다 결국 같은 결말로 간다 — 사람이 게이트를 끈다. 그래서
지표마다 역할을 다르게 준다.

- **품질 지표**(recall/MRR/nDCG/accuracy): 내려가면 실패. 결정적으로 재현되므로
  허용 오차는 부동소수점 오차 수준만 둔다.
- **지연 지표**: 절대 실패시키지 않는다. CI 러너 성능은 실행마다 다르고, 그
  변동으로 빨강이 뜨기 시작하면 진짜 회귀도 같이 무시된다. 2배를 넘으면 경고만
  띄우고 사람이 판단한다.
- **사례 수**: 줄어들면 실패. 실패하는 사례를 지워서 초록을 만드는 것이 가장
  흔하고 가장 조용한 품질 하락 경로다.
- **오라우팅 수**: 늘어나면 실패. 모든 실패가 같은 무게는 아니다 — 되묻기는
  고객이 다시 말하면 회복되지만, 잘못된 갈래로 보내면 고객은 엉뚱한 안내를
  끝까지 듣고 나서야 안다. 정확도가 같아도 이 숫자가 오르면 나빠진 것이다.
- **알려진 한계 수**: 늘어나면 경고. 한계를 새로 고정하는 것은 정당한 선택이지만
  기록 없이 늘어나서는 안 된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOLERANCE = 0.0005
"""품질 지표 허용 오차. 반올림 자릿수(4자리)보다 크고 실제 회귀보다는 작다."""

LATENCY_ALERT_RATIO = 2.0

LATENCY_NOISE_FLOOR_MS = 5.0
"""이 아래에서는 지연 경고를 내지 않는다.

기준선이 0.5ms인데 2배 규칙만 쓰면 러너가 조금만 붐벼도 매번 경고가 뜬다.
매번 뜨는 경고는 경고가 아니라 배경 소음이고, 그 옆에 뜬 진짜 경고도 같이 묻힌다.
"""

QUALITY_METRICS = ("recall_at_k", "mrr", "ndcg_at_k", "accuracy")
LATENCY_METRICS = ("mean_latency_ms", "p95_latency_ms")
COUNT_FLOOR_METRICS = ("total",)
COUNT_CEILING_METRICS = ("misroutes",)
"""늘어나면 실패인 지표. 되묻기는 회복되지만 오라우팅은 고객이 엉뚱한 안내를
끝까지 듣고 나서야 드러난다 — 정확도가 같아도 이 숫자가 오르면 회귀다."""

SuiteMetrics = dict[str, dict[str, float]]


@dataclass(frozen=True)
class Finding:
    """비교 결과 한 줄."""

    suite: str
    metric: str
    baseline: float
    current: float
    message: str
    blocking: bool

    @property
    def delta(self) -> float:
        return round(self.current - self.baseline, 4)


def load_baseline(path: Path) -> SuiteMetrics:
    if not path.is_file():
        return {}
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    suites: Any = raw.get("suites", {}) if isinstance(raw, dict) else {}
    result: SuiteMetrics = {}
    for suite, metrics in suites.items():
        result[str(suite)] = {str(k): float(v) for k, v in metrics.items()}
    return result


def save_baseline(path: Path, suites: SuiteMetrics, *, note: str = "") -> None:
    """베이스라인 기록.

    타임스탬프를 넣지 않는다. 넣으면 숫자가 하나도 안 바뀐 실행에서도 파일이
    바뀌어, diff가 '무엇이 달라졌는가'를 말해 주지 못한다.
    """
    payload = {
        "_comment": "evalctl 회귀 기준선. 갱신은 품질 변화를 의도했을 때만 한다.",
        "note": note,
        "suites": {
            suite: dict(sorted(metrics.items())) for suite, metrics in sorted(suites.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compare(current: SuiteMetrics, baseline: SuiteMetrics) -> list[Finding]:
    """현재 결과를 기준선과 대조한다. ``blocking=True``가 하나라도 있으면 실패다."""
    findings: list[Finding] = []

    for suite in sorted(set(baseline) - set(current)):
        findings.append(
            Finding(
                suite=suite,
                metric="__suite__",
                baseline=1.0,
                current=0.0,
                message="기준선에 있던 스위트가 이번 실행에 없다 — 커버리지가 사라졌다",
                blocking=True,
            )
        )

    for suite in sorted(current):
        base = baseline.get(suite)
        if base is None:
            findings.append(
                Finding(
                    suite=suite,
                    metric="__suite__",
                    baseline=0.0,
                    current=1.0,
                    message="기준선에 없는 새 스위트 — baseline 갱신이 필요하다",
                    blocking=False,
                )
            )
            continue
        findings.extend(_compare_suite(suite, current[suite], base))

    return findings


def _compare_suite(suite: str, current: dict[str, float], base: dict[str, float]) -> list[Finding]:
    findings: list[Finding] = []

    for metric in QUALITY_METRICS:
        if metric not in current or metric not in base:
            continue
        now, before = current[metric], base[metric]
        if now < before - TOLERANCE:
            findings.append(
                Finding(
                    suite=suite,
                    metric=metric,
                    baseline=before,
                    current=now,
                    message=f"품질 하락 {before:.4f} → {now:.4f}",
                    blocking=True,
                )
            )

    for metric in COUNT_FLOOR_METRICS:
        if metric not in current or metric not in base:
            continue
        if current[metric] < base[metric]:
            findings.append(
                Finding(
                    suite=suite,
                    metric=metric,
                    baseline=base[metric],
                    current=current[metric],
                    message=(
                        f"사례가 {int(base[metric])}건 → {int(current[metric])}건으로 줄었다 "
                        "— 실패하는 사례를 지워 초록을 만든 것이 아닌지 본다"
                    ),
                    blocking=True,
                )
            )

    for metric in COUNT_CEILING_METRICS:
        if metric not in current or metric not in base:
            continue
        if current[metric] > base[metric]:
            findings.append(
                Finding(
                    suite=suite,
                    metric=metric,
                    baseline=base[metric],
                    current=current[metric],
                    message=(
                        f"오라우팅이 {int(base[metric])}건 → {int(current[metric])}건으로 늘었다"
                    ),
                    blocking=True,
                )
            )

    if "known_limitations" in current and "known_limitations" in base:
        if current["known_limitations"] > base["known_limitations"]:
            findings.append(
                Finding(
                    suite=suite,
                    metric="known_limitations",
                    baseline=base["known_limitations"],
                    current=current["known_limitations"],
                    message="알려진 한계가 늘었다 — 기록된 판단인지 확인한다",
                    blocking=False,
                )
            )

    for metric in LATENCY_METRICS:
        if metric not in current or metric not in base:
            continue
        now, before = current[metric], base[metric]
        if before > 0 and now > max(before * LATENCY_ALERT_RATIO, LATENCY_NOISE_FLOOR_MS):
            findings.append(
                Finding(
                    suite=suite,
                    metric=metric,
                    baseline=before,
                    current=now,
                    message=(
                        f"지연이 {before:.2f}ms → {now:.2f}ms (러너 성능차일 수 있다, 게이트 아님)"
                    ),
                    blocking=False,
                )
            )

    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.blocking]
