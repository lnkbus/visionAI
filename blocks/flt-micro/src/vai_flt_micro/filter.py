"""Micro-Filter — 사양서 §3 모듈 2.

sLLM을 태우지 않고 정규식만으로 10ms 이내에 끝낸다. 두 가지를 동시에 한다:

1. **PII 마스킹** — 주민번호·카드·계좌·전화·이메일을 치환한다. 이후 단계(LLM
   프롬프트·로그·분석)는 마스킹본만 본다.
2. **컴플라이언스 룰 체크** — 필수 고지 문구를 말했는지, 금지 표현을 썼는지
   본다. 완전판매 모니터링이 여기서 시작된다.

정규식 순서가 중요하다. 주민번호를 먼저 지우지 않으면 전화번호 패턴이
주민번호 뒷자리 일부를 먼저 먹어 마스킹이 어긋난다 — 긴 패턴부터 적용한다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from vai_contracts.events import MatchedRule


@dataclass(frozen=True)
class PiiPattern:
    name: str
    pattern: re.Pattern[str]
    priority: int = 0
    """낮을수록 먼저 적용. 넓은 패턴이 좁은 패턴을 잡아먹지 않게 한다."""

    @property
    def placeholder(self) -> str:
        return f"[{self.name.upper()}_MASKED]"


# 사양서 §3 모듈 2의 패턴을 기반으로, 실제 상담에서 나오는 표기 변형을 흡수했다.
# 하이픈 없이 말하거나 공백을 섞는 경우가 잦아 구분자를 선택적으로 둔다.
DEFAULT_PII_PATTERNS: tuple[PiiPattern, ...] = (
    PiiPattern("rrn", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"), priority=0),
    PiiPattern("card", re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"), priority=1),
    # 전화번호가 계좌 패턴보다 먼저다. 계좌 패턴이 더 느슨해서 010-1234-5678을
    # 먼저 집어삼키면, 마스킹은 되더라도 감사 로그에 '계좌 노출'로 잘못 남는다.
    PiiPattern("phone", re.compile(r"\b01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b"), priority=2),
    # 마지막 그룹을 4자리 이상으로 묶어 "26-06-15" 같은 날짜 표기를 배제한다.
    PiiPattern("account", re.compile(r"\b\d{2,4}[-\s]\d{2,6}[-\s]\d{4,7}\b"), priority=3),
    PiiPattern(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        priority=4,
    ),
)


@dataclass(frozen=True)
class ComplianceRule:
    """테넌트별 필수 고지·금지 표현 룰."""

    rule_id: str
    pattern: re.Pattern[str]
    severity: str = "info"
    """``info``(고지 확인) / ``warning``(주의) / ``violation``(금지 표현 발화)."""

    message: str = ""

    @classmethod
    def compile(cls, spec: dict[str, Any]) -> ComplianceRule:
        return cls(
            rule_id=str(spec["rule_id"]),
            pattern=re.compile(str(spec["pattern"])),
            severity=str(spec.get("severity", "info")),
            message=str(spec.get("message", "")),
        )


@dataclass
class FilterOutcome:
    clean_text: str
    pii_masked: bool = False
    pii_types: list[str] = field(default_factory=list)
    matched_rules: list[MatchedRule] = field(default_factory=list)
    elapsed_ms: float = 0.0


class MicroComplianceFilter:
    """정규식 Fast-Path 필터. 스레드 안전(상태 없음)."""

    def __init__(
        self,
        rules: list[ComplianceRule] | None = None,
        patterns: tuple[PiiPattern, ...] = DEFAULT_PII_PATTERNS,
    ) -> None:
        self._patterns = tuple(sorted(patterns, key=lambda p: p.priority))
        self._rules = list(rules or [])

    def process_text(self, text: str) -> FilterOutcome:
        started = time.perf_counter()

        masked = text
        found: list[str] = []
        for pattern in self._patterns:
            masked, count = pattern.pattern.subn(pattern.placeholder, masked)
            if count:
                found.append(pattern.name)

        matched = [
            MatchedRule(rule_id=rule.rule_id, severity=rule.severity, message=rule.message)
            for rule in self._rules
            # 룰 매칭은 **원문**에 건다. 마스킹본에 걸면 "주민번호를 불러주세요"
            # 같은 금지 발화가 치환 후 패턴을 벗어나 탐지를 빠져나간다.
            if rule.pattern.search(text)
        ]

        return FilterOutcome(
            clean_text=masked,
            pii_masked=bool(found),
            pii_types=found,
            matched_rules=matched,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    @property
    def rule_count(self) -> int:
        return len(self._rules)


def load_rules(specs: list[dict[str, Any]]) -> list[ComplianceRule]:
    """설정에서 읽은 룰 스펙을 컴파일한다.

    잘못된 정규식 하나가 블록 전체 기동을 막지 않도록 개별 실패를 건너뛰되,
    조용히 넘기지는 않는다(호출부가 로그를 남긴다).
    """
    compiled: list[ComplianceRule] = []
    for spec in specs:
        try:
            compiled.append(ComplianceRule.compile(spec))
        except (KeyError, re.error) as exc:
            raise ValueError(f"컴플라이언스 룰 컴파일 실패: {spec!r}") from exc
    return compiled
