"""질의 확장 사전 테스트.

확장은 양날이다 — 다리를 놓기도 하고, 너무 넣으면 질의를 흐린다.
여기서는 그 경계를 고정한다.
"""

from __future__ import annotations

from vai_retrieval.lexicon import DEFAULT_LEXICON, QueryExpander


def test_colloquial_maps_to_formal_terms() -> None:
    expanded, added = QueryExpander().expand("카드 잃어버렸어요 어떻게 하죠")
    assert "분실" in added
    # 원문을 지우지 않는다 — 고객이 정확한 용어를 쓰는 경우의 신호를 잃으면 손해다.
    assert expanded.startswith("카드 잃어버렸어요 어떻게 하죠")


def test_term_already_in_query_is_not_duplicated() -> None:
    """이미 있는 말을 다시 넣으면 그 토큰만 두 배로 세어져 가중치가 왜곡된다."""
    _, added = QueryExpander().expand("분실 신고 어떻게 하나요 잃어버렸어요")
    assert "분실" not in added


def test_expansion_is_capped() -> None:
    query = "카드 잃어버렸고 없애고 싶고 한도도 올리고 미루고 못 쓰고 나눠서 내고 싶어요"
    _, added = QueryExpander(max_expansions=3).expand(query)
    assert len(added) == 3


def test_unknown_wording_adds_nothing() -> None:
    expanded, added = QueryExpander().expand("가맹점 수수료율 산정 기준")
    assert added == []
    assert expanded == "가맹점 수수료율 산정 기준"


def test_disabled_expander_is_a_passthrough() -> None:
    expanded, added = QueryExpander(enabled=False).expand("카드 잃어버렸어요")
    assert (expanded, added) == ("카드 잃어버렸어요", [])


def test_empty_query_is_safe() -> None:
    assert QueryExpander().expand("   ") == ("   ", [])


def test_tenant_lexicon_replaces_the_default() -> None:
    """사업별로 고객이 쓰는 말이 다르다. 기본 사전은 출발점일 뿐이다."""
    expander = QueryExpander(lexicon={"개통": ("가입", "신규")})
    _, added = expander.expand("개통 언제 되나요")
    assert added == ["가입", "신규"]

    _, none_added = expander.expand("카드 잃어버렸어요")
    assert none_added == []


def test_default_lexicon_maps_colloquial_to_formal_not_the_reverse() -> None:
    """방향이 뒤집히면 문서 어휘가 질의에 섞여 아무 조항이나 걸린다."""
    formal_terms = {"분실", "해지", "연기", "상향", "축소", "수수료", "재발급"}
    for trigger in DEFAULT_LEXICON:
        assert trigger not in formal_terms, f"약관 용어가 확장 트리거로 들어갔다: {trigger}"
