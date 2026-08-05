"""의도 분류 테스트.

실패의 방향이 대칭이 아니다. **기권(되묻기)은 회복되지만 오라우팅은 회복되지
않는다** — 고객은 엉뚱한 안내를 끝까지 듣고 나서야 잘못됐음을 안다. 그래서
여기 테스트의 대부분은 "확실하지 않을 때 기권하는가"를 본다.
"""

from __future__ import annotations

from vai_bot_voice.classify import (
    ABSTAIN,
    ChainClassifier,
    ExampleSimilarityClassifier,
    SlmIntentClassifier,
    declared,
    parse_choice,
    render_prompt,
)
from vai_contracts.dialog import DialogNode, Intent, NodeKind

CONFIRM = Intent(
    name="confirm",
    patterns=["^(네|예)"],
    next_node="done",
    description="진행하겠다는 뜻",
    examples=["네 맞아요", "그렇게 해주세요", "진행해 주세요"],
)
DENY = Intent(
    name="deny",
    patterns=["^아니"],
    next_node="bye",
    description="진행하지 않겠다는 뜻",
    examples=["아니요 괜찮아요", "안 할게요", "나중에 할게요"],
)


def node(*intents: Intent) -> DialogNode:
    return DialogNode(node_id="ask", kind=NodeKind.ASK, intents=list(intents))


# ── 어휘 분류기 ──────────────────────────────────────────────────────────────


async def test_paraphrase_is_routed() -> None:
    chosen = await ExampleSimilarityClassifier().classify(
        node(CONFIRM, DENY), "그렇게 해 주시겠어요"
    )
    assert chosen == "confirm"


async def test_unrelated_utterance_abstains() -> None:
    chosen = await ExampleSimilarityClassifier().classify(
        node(CONFIRM, DENY), "주차장은 어디에 있나요"
    )
    assert chosen is None


async def test_close_scores_abstain() -> None:
    """1등과 2등이 붙어 있으면 '둘 중 하나'이지 '이것'이 아니다."""
    twin_a = Intent(name="a", examples=["카드 재발급 신청합니다"])
    twin_b = Intent(name="b", examples=["카드 재발급 문의합니다"])
    chosen = await ExampleSimilarityClassifier().classify(node(twin_a, twin_b), "카드 재발급이요")
    assert chosen is None


async def test_no_examples_means_no_opinion() -> None:
    """저작자가 예시를 안 적었으면 판단할 재료가 없다. 지어내지 않는다."""
    bare = Intent(name="confirm", patterns=["^네$"])
    chosen = await ExampleSimilarityClassifier().classify(node(bare), "그렇게 해주세요")
    assert chosen is None


async def test_empty_utterance_abstains() -> None:
    assert await ExampleSimilarityClassifier().classify(node(CONFIRM, DENY), "   ") is None


async def test_many_examples_do_not_dilute_the_score() -> None:
    """예시를 성실히 늘릴수록 분류가 나빠지면 저작자가 예시를 안 적게 된다."""
    few = node(
        Intent(name="confirm", examples=["그렇게 해주세요"]),
        Intent(name="deny", examples=["안 할게요"]),
    )
    many = node(
        Intent(
            name="confirm",
            examples=["그렇게 해주세요", "진행해 주세요", "네 맞아요", "부탁드려요", "좋아요"],
        ),
        Intent(name="deny", examples=["안 할게요"]),
    )
    classifier = ExampleSimilarityClassifier()
    assert await classifier.classify(few, "그렇게 해 주시겠어요") == "confirm"
    assert await classifier.classify(many, "그렇게 해 주시겠어요") == "confirm"


# ── sLLM 분류기 ──────────────────────────────────────────────────────────────


async def test_slm_choice_is_used() -> None:
    async def model(prompt: str, system: str = "") -> str:
        assert "confirm" in prompt and ABSTAIN in prompt
        return "confirm"

    assert await SlmIntentClassifier(model).classify(node(CONFIRM, DENY), "해줘요") == "confirm"


async def test_slm_abstention_is_respected() -> None:
    async def model(prompt: str, system: str = "") -> str:
        return ABSTAIN

    assert await SlmIntentClassifier(model).classify(node(CONFIRM, DENY), "글쎄요") is None


async def test_slm_invented_intent_is_dropped() -> None:
    """시나리오에 없는 갈래를 지어내면 그건 분류기가 아니라 또 하나의 봇이다."""

    async def model(prompt: str, system: str = "") -> str:
        return "cancel_everything"

    assert await SlmIntentClassifier(model).classify(node(CONFIRM, DENY), "해줘요") is None


async def test_slm_failure_falls_back_to_reprompt() -> None:
    async def broken(prompt: str, system: str = "") -> str:
        raise RuntimeError("모델 다운")

    assert await SlmIntentClassifier(broken).classify(node(CONFIRM, DENY), "해줘요") is None


def test_parse_choice_tolerates_decoration() -> None:
    target = node(CONFIRM, DENY)
    assert parse_choice(" confirm ", target) == "confirm"
    assert parse_choice('"deny"', target) == "deny"
    assert parse_choice("정답: confirm", target) == "confirm"


def test_parse_choice_abstains_when_ambiguous() -> None:
    """이름이 둘 다 보이면 무엇을 고른 것인지 알 수 없다."""
    assert parse_choice("confirm 또는 deny", node(CONFIRM, DENY)) is None
    assert parse_choice("", node(CONFIRM, DENY)) is None
    assert parse_choice(ABSTAIN, node(CONFIRM, DENY)) is None


# ── 체인 ─────────────────────────────────────────────────────────────────────


async def test_chain_stops_at_the_first_answer() -> None:
    calls: list[str] = []

    class Stage(ExampleSimilarityClassifier):
        def __init__(self, label: str, answer: str | None) -> None:
            super().__init__()
            self._label, self._answer = label, answer
            self.name = label

        async def classify(self, node_: DialogNode, text: str) -> str | None:
            calls.append(self._label)
            return self._answer

    chain = ChainClassifier(Stage("cheap", "confirm"), Stage("expensive", "deny"))

    assert await chain.classify(node(CONFIRM, DENY), "해줘요") == "confirm"
    assert calls == ["cheap"], "앞 단계가 답하면 비싼 단계는 부르지 않는다"


async def test_chain_skips_undeclared_answers() -> None:
    class Liar(ExampleSimilarityClassifier):
        name = "liar"

        async def classify(self, node_: DialogNode, text: str) -> str | None:
            return "nonexistent"

    chain = ChainClassifier(Liar(), ExampleSimilarityClassifier())

    assert await chain.classify(node(CONFIRM, DENY), "그렇게 해 주시겠어요") == "confirm"


def test_declared_guards_the_route() -> None:
    target = node(CONFIRM, DENY)
    assert declared(target, "confirm") is CONFIRM
    assert declared(target, "nonexistent") is None


def test_prompt_lists_the_abstain_option() -> None:
    """기권할 길이 없으면 모델은 반드시 무언가를 고른다 — 그게 오라우팅이 된다."""
    prompt = render_prompt(node(CONFIRM, DENY), "해줘요")
    assert ABSTAIN in prompt
    assert "진행하겠다는 뜻" in prompt
