"""질의 추출 테스트.

SLM이 붙든 안 붙든 팝업이 계속 떠야 한다는 것이 이 모듈의 핵심 요구다.
"""

from __future__ import annotations

import pytest

from vai_contracts.session import ChannelRole
from vai_ta_assist.extractor import CONTEXT_TURNS, ConversationWindow, QueryExtractor, should_search


def window(*turns: tuple[ChannelRole, str]) -> ConversationWindow:
    win = ConversationWindow()
    for role, text in turns:
        win.add(role, text)
    return win


async def test_falls_back_to_the_customer_question_without_slm() -> None:
    win = window(
        (ChannelRole.AGENT, "무엇을 도와드릴까요?"),
        (ChannelRole.CUSTOMER, "이번 달 결제일 연기가 가능한가요?"),
    )

    assert await QueryExtractor().extract(win) == "이번 달 결제일 연기가 가능한가요?"


async def test_agent_utterances_are_not_used_as_the_query() -> None:
    """상담원 발화를 질의로 쓰면 이미 한 답변을 다시 검색하게 된다."""
    win = window(
        (ChannelRole.CUSTOMER, "결제일 연기 되나요?"),
        (ChannelRole.AGENT, "네, 최대 5일까지 가능합니다."),
    )

    assert await QueryExtractor().extract(win) == "결제일 연기 되나요?"


async def test_masked_tokens_are_stripped_from_the_query() -> None:
    """마스킹 토큰은 어느 문서에도 없어 BM25 점수만 흐린다."""
    win = window((ChannelRole.CUSTOMER, "제 번호는 [RRN_MASKED] 인데 결제일 연기 되나요?"))

    query = await QueryExtractor().extract(win)

    assert "MASKED" not in query
    assert "결제일" in query


async def test_slm_output_is_preferred_when_available() -> None:
    async def complete(prompt: str, system: str) -> str:
        return "카드 결제일 연기 신청 조건"

    query = await QueryExtractor(complete).extract(
        window((ChannelRole.CUSTOMER, "아 그거 좀 미룰 수 있나요"))
    )

    assert query == "카드 결제일 연기 신청 조건"


async def test_slm_failure_does_not_kill_the_popup() -> None:
    """SLM 장애로 팝업이 통째로 사라지면 상담원은 시스템이 죽은 줄 안다."""

    async def broken(prompt: str, system: str) -> str:
        raise RuntimeError("vLLM 응답 없음")

    query = await QueryExtractor(broken).extract(
        window((ChannelRole.CUSTOMER, "결제일 연기 되나요?"))
    )

    assert query == "결제일 연기 되나요?"


async def test_empty_slm_output_falls_back() -> None:
    async def blank(prompt: str, system: str) -> str:
        return "   "

    query = await QueryExtractor(blank).extract(
        window((ChannelRole.CUSTOMER, "연회비 면제 조건이 뭔가요?"))
    )

    assert query == "연회비 면제 조건이 뭔가요?"


async def test_rambling_slm_output_falls_back() -> None:
    """지시를 무시하고 설명을 늘어놓는 응답을 그대로 검색에 넣으면 안 된다."""

    async def rambling(prompt: str, system: str) -> str:
        return "이 질문에 대해 설명드리자면 " * 50

    query = await QueryExtractor(rambling).extract(
        window((ChannelRole.CUSTOMER, "연회비 면제 조건이 뭔가요?"))
    )

    assert query == "연회비 면제 조건이 뭔가요?"


def test_window_keeps_only_the_recent_turns() -> None:
    """사양서가 지정한 맥락 창은 최근 3문장이다."""
    win = window(*[(ChannelRole.CUSTOMER, f"발화 {i}") for i in range(10)])

    assert len(win.turns) == CONTEXT_TURNS
    assert win.latest == "발화 9"


def test_rendered_context_labels_the_speakers() -> None:
    rendered = window(
        (ChannelRole.CUSTOMER, "질문입니다"), (ChannelRole.AGENT, "답변입니다")
    ).render()

    assert rendered == "고객: 질문입니다\n상담원: 답변입니다"


@pytest.mark.parametrize("filler", ["네", "예", "음", "아", "감사합니다", "  "])
def test_backchannel_does_not_trigger_search(filler: str) -> None:
    """맞장구마다 SLM과 검색을 돌리면 GPU와 지연 예산을 맞장구에 쓴다."""
    assert not should_search(filler)


def test_real_question_triggers_search() -> None:
    assert should_search("결제일 연기가 가능한가요?")
