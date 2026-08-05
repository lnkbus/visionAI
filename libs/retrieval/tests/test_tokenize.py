"""토크나이저 교체 경로.

**색인과 질의가 같은 토크나이저를 써야 한다.** 어긋나면 BM25도 해싱 임베딩도
틀어지는데 오류는 나지 않는다 — "검색이 좀 이상하다"로만 나타나고, 그 상태가
가장 오래 간다.
"""

from __future__ import annotations

import pytest

from vai_retrieval.tokenize import (
    SyllableTokenizer,
    active_tokenizer,
    create_tokenizer,
    set_tokenizer,
    tokenize,
)


@pytest.fixture(autouse=True)
def _restore_default():
    """전역 상태를 건드리는 테스트다. 다음 테스트로 새어 나가면 안 된다."""
    before = active_tokenizer()
    yield
    set_tokenizer(before)


def test_default_is_the_syllable_tokenizer() -> None:
    """기본값을 바꾸는 것은 근거 검증·의도 분류 임계값까지 함께 바꾸는 일이다."""
    assert active_tokenizer().name == "syllable"


def test_syllable_keeps_exact_words_and_absorbs_particles() -> None:
    tokens = SyllableTokenizer().tokenize("결제일이 언제인가요")

    assert "결제일이" in tokens, "정확한 어절이 남아야 키워드 매칭이 된다"
    assert "결제" in tokens, "바이그램이 조사 변형을 흡수한다"


def test_unknown_tokenizer_fails_loudly() -> None:
    with pytest.raises(ValueError, match="syllable"):
        create_tokenizer("형태소분석기_없음")


def test_setting_a_tokenizer_changes_the_shared_function() -> None:
    """모듈 함수를 이미 import 한 곳(bm25·리랭커·임베더)도 함께 바뀌어야 한다.

    한쪽만 바뀌면 색인과 질의가 갈리고, 그건 오류 없이 검색만 나빠진다.
    """

    class Upper(SyllableTokenizer):
        name = "upper"

        def tokenize(self, text: str) -> list[str]:
            return [text.upper()]

    set_tokenizer(Upper())

    assert tokenize("가나") == ["가나"], "전역 교체가 모듈 함수에 반영돼야 한다"


def test_kiwi_keeps_prefixes_that_change_meaning() -> None:
    """형태소만 남기면 "재발급"이 "발급"이 된다 — 약관에서는 다른 조항이다."""
    pytest.importorskip("kiwipiepy")

    tokens = create_tokenizer("kiwi").tokenize("카드 재발급 신청")

    assert "재발급" in tokens
