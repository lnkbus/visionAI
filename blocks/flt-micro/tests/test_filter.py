"""Micro-Filter 계약 테스트.

이 블록이 새는 것은 곧 개인정보 유출이므로, 통과 케이스보다 **빠져나가는
케이스**를 더 많이 확인한다.
"""

from __future__ import annotations

import pytest

from vai_flt_micro.filter import MicroComplianceFilter, load_rules


@pytest.fixture
def plain() -> MicroComplianceFilter:
    return MicroComplianceFilter()


@pytest.mark.parametrize(
    ("text", "expected_type"),
    [
        ("제 주민등록번호는 900101-1234567 입니다", "rrn"),
        ("주민번호 9001011234567 이요", "rrn"),
        ("카드번호 1234-5678-9012-3456 입니다", "card"),
        ("카드 1234 5678 9012 3456", "card"),
        ("연락처는 010-1234-5678 입니다", "phone"),
        ("전화 01012345678", "phone"),
        ("메일은 hong@example.com 입니다", "email"),
        ("계좌 110-234-567890 으로 보내주세요", "account"),
    ],
)
def test_pii_is_masked(plain: MicroComplianceFilter, text: str, expected_type: str) -> None:
    outcome = plain.process_text(text)

    assert outcome.pii_masked
    assert expected_type in outcome.pii_types
    assert f"[{expected_type.upper()}_MASKED]" in outcome.clean_text


def test_original_digits_do_not_survive_masking(plain: MicroComplianceFilter) -> None:
    outcome = plain.process_text("주민번호 900101-1234567, 카드 1234-5678-9012-3456")

    assert "900101" not in outcome.clean_text
    assert "1234567" not in outcome.clean_text
    assert "9012" not in outcome.clean_text


def test_rrn_is_masked_before_phone_pattern_can_eat_it(plain: MicroComplianceFilter) -> None:
    """패턴 우선순위가 뒤집히면 주민번호가 부분 마스킹으로 새어 나간다."""
    outcome = plain.process_text("900101-1234567")

    assert outcome.clean_text == "[RRN_MASKED]"
    assert outcome.pii_types == ["rrn"]


def test_phone_is_not_misclassified_as_account(plain: MicroComplianceFilter) -> None:
    """계좌 패턴이 더 느슨해 순서가 뒤집히면 감사 로그에 '계좌 노출'로 잘못 남는다."""
    outcome = plain.process_text("연락처는 010-1234-5678 입니다")

    assert outcome.pii_types == ["phone"]
    assert "[PHONE_MASKED]" in outcome.clean_text


def test_date_notation_is_not_mistaken_for_an_account(plain: MicroComplianceFilter) -> None:
    outcome = plain.process_text("계약일은 26-06-15 입니다")

    assert not outcome.pii_masked
    assert "26-06-15" in outcome.clean_text


def test_ordinary_numbers_are_left_alone(plain: MicroComplianceFilter) -> None:
    """과잉 마스킹은 상담 기록을 못 읽게 만든다."""
    outcome = plain.process_text("보험료가 월 35,000원이고 납입기간은 20년입니다")

    assert not outcome.pii_masked
    assert "35,000" in outcome.clean_text


def test_clean_text_is_returned_untouched_when_nothing_matches(
    plain: MicroComplianceFilter,
) -> None:
    text = "이번 달 카드 대금 결제일 연기가 가능한가요?"

    assert plain.process_text(text).clean_text == text


def test_fast_path_stays_within_budget(plain: MicroComplianceFilter) -> None:
    """사양서 §3 모듈 2의 10ms 예산. 정규식이 잘못 짜이면 여기서 걸린다."""
    text = "안녕하세요 " * 200 + "주민번호 900101-1234567 카드 1234-5678-9012-3456"

    outcome = plain.process_text(text)

    assert outcome.elapsed_ms < 10.0, f"Fast-Path 예산 초과: {outcome.elapsed_ms:.2f}ms"


# ── 컴플라이언스 룰 ──────────────────────────────────────────────────────────

RULES = [
    {
        "rule_id": "MANDATORY_RECORDING",
        "pattern": r"녹취|녹음.*안내",
        "severity": "info",
        "message": "녹취 고지 확인",
    },
    {
        "rule_id": "FORBIDDEN_GUARANTEE",
        "pattern": r"원금.?보장|무조건.?수익|손실.?없",
        "severity": "violation",
        "message": "확정 수익 표현은 금지",
    },
    {
        "rule_id": "PII_SOLICITATION",
        "pattern": r"주민(등록)?번호.*(불러|말씀|알려)",
        "severity": "warning",
        "message": "음성으로 주민번호를 받지 않는다",
    },
]


@pytest.fixture
def ruled() -> MicroComplianceFilter:
    return MicroComplianceFilter(load_rules(RULES))


def test_mandatory_notice_is_detected(ruled: MicroComplianceFilter) -> None:
    outcome = ruled.process_text("본 상담은 녹취되고 있습니다")

    assert [rule.rule_id for rule in outcome.matched_rules] == ["MANDATORY_RECORDING"]


def test_forbidden_expression_is_flagged_as_violation(ruled: MicroComplianceFilter) -> None:
    outcome = ruled.process_text("이 상품은 원금 보장이 됩니다")

    violation = next(r for r in outcome.matched_rules if r.rule_id == "FORBIDDEN_GUARANTEE")
    assert violation.severity == "violation"


def test_rules_match_the_original_not_the_masked_text(ruled: MicroComplianceFilter) -> None:
    """마스킹본에 룰을 걸면 치환으로 패턴이 깨져 탐지를 빠져나간다."""
    outcome = ruled.process_text("고객님 주민등록번호를 불러주세요. 900101-1234567 맞으신가요?")

    assert outcome.pii_masked, "번호는 마스킹되어야 하고"
    assert any(r.rule_id == "PII_SOLICITATION" for r in outcome.matched_rules), (
        "요구 행위 자체는 탐지되어야 한다"
    )


def test_invalid_rule_fails_loudly() -> None:
    """잘못된 정규식이 조용히 무시되면 룰이 없는 줄 모르고 운영하게 된다."""
    with pytest.raises(ValueError, match="컴플라이언스 룰"):
        load_rules([{"rule_id": "BROKEN", "pattern": "([unclosed"}])
