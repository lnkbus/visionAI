"""질의 추출 — 사양서 §3 모듈 3의 1단계 (SLM, <200ms).

상담 발화를 그대로 검색에 넣으면 안 된다. "아 그게 저기 이번 달에 그거 좀
미룰 수 있나요"에서 검색에 쓸 만한 신호는 "이번 달", "미루다"뿐이고, 앞뒤
발화를 봐야 "카드 결제일 연기"라는 의도가 나온다. SLM이 최근 3문장 맥락에서
검색용 질의 한 줄을 뽑는 이유다.

SLM이 없거나 느릴 때를 대비해 어휘 기반 폴백을 둔다. 팝업이 조금 부정확한
것보다 아예 안 뜨는 게 나쁘다 — 상담원은 기다려 주지 않는다.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field

from vai_contracts.session import ChannelRole
from vai_ta_assist.ports import CompletePort

log = logging.getLogger(__name__)

CONTEXT_TURNS = 3
"""사양서가 지정한 맥락 창: 최근 3문장."""

SYSTEM_PROMPT = (
    "너는 콜센터 상담 보조 시스템의 질의 추출기다. "
    "주어진 최근 대화에서 지식베이스 검색에 쓸 핵심 질의를 한 줄로 뽑아라. "
    "설명하지 말고 검색어만 출력한다. 개인정보나 마스킹 토큰은 포함하지 않는다."
)

# 마스킹 토큰은 검색에 쓸모가 없을 뿐 아니라 모든 문서에 없는 토큰이라
# BM25 점수를 흐린다.
_MASK_TOKEN = re.compile(r"\[[A-Z_]+_MASKED\]")
_QUESTION_HINT = re.compile(r"(나요|까요|가요|인가요|되나요|하죠|\?)\s*$")


@dataclass
class ConversationWindow:
    """세션별 최근 발화 창."""

    turns: deque[tuple[ChannelRole, str]] = field(
        default_factory=lambda: deque(maxlen=CONTEXT_TURNS)
    )

    def add(self, channel: ChannelRole, text: str) -> None:
        self.turns.append((channel, text))

    def render(self) -> str:
        labels = {ChannelRole.CUSTOMER: "고객", ChannelRole.AGENT: "상담원"}
        return "\n".join(f"{labels.get(role, '참여자')}: {text}" for role, text in self.turns)

    @property
    def latest(self) -> str:
        return self.turns[-1][1] if self.turns else ""


class QueryExtractor:
    """맥락 → 검색 질의."""

    def __init__(self, complete: CompletePort | None = None, *, max_chars: int = 120) -> None:
        self._complete = complete
        """LLM-GW 클라이언트. None이면 어휘 폴백만 쓴다."""

        self._max_chars = max_chars

    async def extract(self, window: ConversationWindow) -> str:
        heuristic = self._heuristic(window)
        if self._complete is None:
            return heuristic

        try:
            text = await self._complete(window.render(), SYSTEM_PROMPT)
        except Exception:
            # SLM 장애로 팝업 전체를 죽이지 않는다. 품질은 떨어져도 계속 뜬다.
            log.warning("SLM 질의 추출 실패 — 어휘 폴백 사용", exc_info=True)
            return heuristic

        cleaned = _MASK_TOKEN.sub(" ", str(text)).strip()
        # SLM이 빈 문자열이나 장황한 설명을 내놓는 경우가 있어 검증 후 채택한다.
        if not cleaned or len(cleaned) > self._max_chars * 3:
            return heuristic
        return cleaned[: self._max_chars]

    def _heuristic(self, window: ConversationWindow) -> str:
        """어휘 폴백: 마지막 고객 질문을 우선 쓰고, 없으면 최근 발화를 쓴다."""
        customer_turns = [text for role, text in window.turns if role is ChannelRole.CUSTOMER]
        candidates = [text for text in customer_turns if _QUESTION_HINT.search(text)]
        chosen = (
            candidates[-1]
            if candidates
            else (customer_turns[-1] if customer_turns else window.latest)
        )
        return _MASK_TOKEN.sub(" ", chosen).strip()[: self._max_chars]


def should_search(text: str, *, min_chars: int = 6) -> bool:
    """검색을 걸 만한 발화인지 판단한다.

    "네", "아 예" 같은 맞장구마다 SLM과 검색을 돌리면 GPU와 지연 예산을
    맞장구에 쓰게 된다.
    """
    stripped = _MASK_TOKEN.sub("", text).strip()
    if len(stripped) < min_chars:
        return False
    filler = {"네", "예", "아", "음", "그렇군요", "알겠습니다", "감사합니다"}
    return stripped not in filler
