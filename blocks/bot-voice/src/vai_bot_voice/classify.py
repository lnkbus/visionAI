"""의도 분류 — 정규식이 놓친 발화를 건져낸다.

정규식은 고객이 저작자가 예상한 말투로 답할 때만 맞는다. "네"는 잡지만
"그렇게 해주세요", "응 맞아요", "해줘"는 놓친다. 놓치면 되묻고, 두 번 되물으면
사람에게 넘어간다 — 봇이 있으나 마나 한 상태가 이렇게 만들어진다.

**그렇다고 아무 갈래나 고르게 하면 더 나쁘다.** 되묻기는 고객이 다시 말하면
회복되지만, 잘못 라우팅되면 고객은 엉뚱한 안내를 끝까지 듣고 나서야 잘못됐음을
안다. 그래서 이 계층의 제1 규칙은 **확실하지 않으면 기권한다**이다.

세 단계로 둔다:

1. **정규식** (0ms, 결정적) — 저작자가 명시한 우선순위가 그대로 이긴다.
2. **어휘 유사도** (모델 없음) — 저작자가 적어 둔 예시 발화와 겹치는 정도.
   GPU가 없는 소형 프로파일에서도 이 계층까지는 돈다.
3. **sLLM** (선택) — 위 둘이 기권했을 때만 부른다. 즉 **되묻게 될 발화에서만**
   비용을 낸다.

어느 단계든 결과는 **노드에 선언된 인텐트 이름**이어야 한다. 분류기가 없는
이름을 내놓으면 무시한다 — 분류기가 경로를 만들어 낼 수는 없다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from vai_contracts.dialog import DialogNode, Intent
from vai_retrieval.tokenize import tokenize

log = logging.getLogger(__name__)

ABSTAIN = "모름"
"""분류기가 기권할 때 쓰는 이름. 인텐트 이름으로 쓰면 안 된다."""

CompletePort = Callable[[str, str], Awaitable[str]]


class BaseIntentClassifier(ABC):
    """의도 분류기. 확신이 없으면 ``None``을 돌려준다."""

    name: str

    @abstractmethod
    async def classify(self, node: DialogNode, text: str) -> str | None:
        """인텐트 이름 또는 ``None``(기권)."""


def declared(node: DialogNode, name: str) -> Intent | None:
    """노드에 실제로 선언된 인텐트인지 확인한다.

    분류기의 출력을 그대로 믿지 않는 지점이다. 없는 이름이 통과하면 시나리오
    검증(도달 가능한 handoff, 인텐트별 next_node)이 보장하던 것이 전부 무너진다.
    """
    return next((intent for intent in node.intents if intent.name == name), None)


def _similarity(left: set[str], right: set[str]) -> float:
    """겹침 계수(overlap coefficient). 짧은 쪽 크기로 정규화한다.

    자카드(합집합으로 나누기)를 쓰면 길이가 다른 두 문장이 실제로 같은 말을
    해도 점수가 크게 깎인다 — "직원분이랑 통화하고 싶은데요"와 "사람이랑
    통화할래요"가 그런 경우다. 실측에서 자카드는 라우팅해야 할 발화와 기권해야
    할 발화의 점수대가 겹쳤고(0.10 vs 0.06), 겹침 계수는 갈라졌다(0.22 vs 0.20 —
    여유까지 보면 확실히).

    다만 짧은 쪽이 한 토큰이면 토큰 하나만 겹쳐도 1.0이 된다. 그 구간에서는
    자카드로 되돌려 과대평가를 막는다.
    """
    if not left or not right:
        return 0.0
    shorter = min(len(left), len(right))
    if shorter < 2:
        return len(left & right) / len(left | right)
    return len(left & right) / shorter


def _best_example_score(intent: Intent, tokens: set[str]) -> float:
    """예시 **하나하나와** 비교해 가장 닮은 것의 점수를 쓴다.

    예시를 전부 합쳐 하나의 덩어리로 보면(중심점 방식), 예시를 많이 적을수록
    덩어리가 커져 점수가 떨어진다. 저작자가 성실하게 예시를 늘릴수록 분류가
    나빠지는 셈이라 그 방식은 쓸 수 없다. "어느 예시와 닮았는가"가 옳은 질문이다.

    설명(``description``)도 후보에 넣되 예시와 같은 자격으로만 본다 — 설명은
    보통 문어체라 실제 발화와 덜 겹친다.
    """
    candidates = [*intent.examples, intent.description, intent.name]
    return max(
        (_similarity(tokens, set(tokenize(candidate))) for candidate in candidates if candidate),
        default=0.0,
    )


class ExampleSimilarityClassifier(BaseIntentClassifier):
    """저작자가 적어 둔 예시 발화와의 어휘 겹침으로 고른다. 모델이 필요 없다.

    :class:`~vai_retrieval.rerank.LexicalOverlapReranker`와 같은 자리의 구현이다 —
    GPU 없이도 파이프라인이 온전히 돌아야 하고, 실제로 쓸 만한 하한을 준다.

    **여유(margin)를 요구하는 것이 핵심이다.** 1등과 2등 점수가 붙어 있으면
    그것은 "둘 중 하나"이지 "이것"이 아니다. 그 상태에서 하나를 고르면 절반은
    틀리고, 틀린 절반은 고객이 엉뚱한 안내를 끝까지 듣는다. 되묻는 편이 낫다.
    """

    name = "lexical"

    def __init__(self, *, min_score: float = 0.20, margin: float = 0.09) -> None:
        self._min_score = min_score
        self._margin = margin

    async def classify(self, node: DialogNode, text: str) -> str | None:
        tokens = set(tokenize(text))
        if not tokens:
            return None

        scored: list[tuple[float, str]] = []
        for intent in node.intents:
            score = _best_example_score(intent, tokens)
            if score > 0:
                scored.append((score, intent.name))

        if not scored:
            return None
        scored.sort(reverse=True)
        best_score, best_name = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0

        if best_score < self._min_score or best_score - runner_up < self._margin:
            log.debug(
                "어휘 분류 기권",
                extra={"best": round(best_score, 3), "runner_up": round(runner_up, 3)},
            )
            return None
        return best_name


SYSTEM_PROMPT = (
    "너는 콜센터 음성봇의 의도 분류기다. "
    "고객 발화가 아래 선택지 중 어디에 해당하는지 이름 하나만 출력한다. "
    f"확실하지 않으면 '{ABSTAIN}'이라고만 출력한다. "
    "설명하거나 문장을 만들지 않는다."
)


def render_prompt(node: DialogNode, text: str) -> str:
    lines = ["[선택지]"]
    for intent in node.intents:
        meaning = intent.description or intent.name
        lines.append(f"- {intent.name}: {meaning}")
        if intent.examples:
            lines.append(f"  예: {' / '.join(intent.examples[:5])}")
    lines += [f"- {ABSTAIN}: 위 어느 것도 아니거나 판단하기 어렵다", "", "[고객 발화]", text, ""]
    lines.append("[정답]")
    return "\n".join(lines)


def parse_choice(raw: str, node: DialogNode) -> str | None:
    """모델 출력에서 인텐트 이름을 꺼낸다. 애매하면 기권한다."""
    cleaned = raw.strip().strip("\"'`[]() .·:").splitlines()[0].strip() if raw.strip() else ""
    if not cleaned or cleaned == ABSTAIN:
        return None

    names = [intent.name for intent in node.intents]
    if cleaned in names:
        return cleaned

    # 모델이 "정답: confirm" 처럼 덧붙이는 경우가 있다. 이름이 **하나만** 보이면
    # 받아들이고, 둘 이상 보이면 무엇을 고른 것인지 알 수 없으므로 기권한다.
    found = [name for name in names if name in cleaned]
    if len(found) == 1:
        return found[0]
    return None


class SlmIntentClassifier(BaseIntentClassifier):
    """sLLM 분류기. 선택지 안에서만 고르게 하고, 기권을 명시적으로 허용한다.

    생성이 아니라 **선택**이다. 자유 서술을 시키면 시나리오에 없는 갈래를
    지어내고, 그건 분류기가 아니라 또 하나의 봇이 된다.
    """

    name = "slm"

    def __init__(self, complete: CompletePort) -> None:
        self._complete = complete

    async def classify(self, node: DialogNode, text: str) -> str | None:
        if not node.intents or not text.strip():
            return None
        try:
            raw = await self._complete(render_prompt(node, text), SYSTEM_PROMPT)
        except Exception:
            # 분류 실패는 되묻기로 흡수된다. 통화를 끊을 이유가 아니다.
            log.warning("sLLM 의도 분류 실패 — 되묻기로 진행", exc_info=True)
            return None
        return parse_choice(str(raw), node)


class ChainClassifier(BaseIntentClassifier):
    """앞 단계가 기권하면 다음 단계로 넘긴다.

    순서가 곧 비용 순서다. 어휘 분류가 답하면 sLLM은 호출되지 않는다.
    """

    name = "chain"

    def __init__(self, *stages: BaseIntentClassifier) -> None:
        self._stages = stages

    async def classify(self, node: DialogNode, text: str) -> str | None:
        for stage in self._stages:
            chosen = await stage.classify(node, text)
            if chosen is None:
                continue
            if declared(node, chosen) is None:
                # 분류기가 없는 갈래를 내놓았다. 경로를 만들어 낼 수는 없다.
                log.warning(
                    "선언되지 않은 인텐트 무시",
                    extra={"node": node.node_id, "chosen": chosen, "stage": stage.name},
                )
                continue
            return chosen
        return None
