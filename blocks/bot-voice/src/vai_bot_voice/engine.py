"""대화 엔진 — 시나리오를 한 걸음씩 진행한다.

버스·저장소와 무관한 순수 상태 기계다. 음성봇에서 가장 자주 깨지는 것은
모델이 아니라 **막다른 골목**이다: 못 알아들어 되묻고, 또 못 알아들어 되묻고,
고객은 결국 끊는다. 그 경계를 여기서 단위 테스트로 고정한다.

세 가지를 불변으로 지킨다.

1. **재질문에는 상한이 있다.** 넘으면 사람에게 넘긴다.
2. **어떤 노드에서도 사람에게 갈 길이 있다.** 빠져나갈 곳 없는 봇은 고객을 가둔다.
3. **되묻는 말은 처음과 달라야 한다.** 같은 문장을 반복하면 고객은 봇이
   고장 났다고 여기고 말을 멈춘다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from vai_contracts.dialog import DialogNode, DialogState, Intent, NodeKind, Scenario

log = logging.getLogger(__name__)


@dataclass
class Utterance:
    """봇이 낼 한 마디."""

    node_id: str
    text: str
    expects_reply: bool = False
    handoff: bool = False
    ends_call: bool = False


@dataclass
class StepResult:
    """한 걸음의 결과. 여러 마디가 이어질 수 있다(say → say → ask)."""

    utterances: list[Utterance] = field(default_factory=list)
    state: DialogState | None = None

    @property
    def text(self) -> str:
        return " ".join(u.text for u in self.utterances if u.text)


class ScenarioError(ValueError):
    """시나리오가 잘못됐다. 배포 전에 잡아야 하는 것들이다."""


MAX_CHAIN = 12
"""한 걸음에 이을 수 있는 노드 수. 넘으면 순환이다 — 고객은 봇이 혼자
끝없이 말하는 것을 듣게 되므로 반드시 끊는다."""


def validate(scenario: Scenario) -> list[str]:
    """배포 전 검사. 저작 도구가 호출한다.

    운영 중 발견하면 이미 고객이 갇힌 뒤다.
    """
    problems: list[str] = []
    ids = {node.node_id for node in scenario.nodes}

    if not scenario.nodes:
        return ["노드가 없다"]
    if scenario.entry_node not in ids:
        problems.append(f"시작 노드 '{scenario.entry_node}'가 없다")
    if scenario.fallback_node and scenario.fallback_node not in ids:
        problems.append(f"폴백 노드 '{scenario.fallback_node}'가 없다")

    for node in scenario.nodes:
        for target, label in _targets(node):
            if target and target not in ids:
                problems.append(f"{node.node_id}: {label} 대상 '{target}'가 없다")
        if node.kind is NodeKind.ASK and not node.intents:
            problems.append(f"{node.node_id}: ask인데 인텐트가 없다 — 답을 받아도 갈 곳이 없다")
        if node.kind is NodeKind.ASK and not node.reprompt:
            # 같은 문장을 반복하면 고객은 봇이 고장 났다고 여긴다.
            problems.append(f"{node.node_id}: 재질문 문구가 없다")
        for intent in node.intents:
            for pattern in intent.patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(f"{node.node_id}/{intent.name}: 정규식 오류 — {exc}")

    if not _reaches_handoff(scenario, ids):
        # 빠져나갈 곳 없는 봇은 고객을 가둔다. 이것 하나로 브랜드가 상한다.
        problems.append("사람 상담원으로 갈 수 있는 경로가 없다")
    return problems


def _targets(node: DialogNode) -> list[tuple[str, str]]:
    out = [(node.next_node, "next")]
    out += [(intent.next_node, f"intent:{intent.name}") for intent in node.intents]
    return out


def _reaches_handoff(scenario: Scenario, ids: set[str]) -> bool:
    if not any(n.kind is NodeKind.HANDOFF for n in scenario.nodes):
        return False
    # 도달 가능성까지 본다. 어디서도 갈 수 없는 handoff 노드는 없는 것과 같다.
    seen: set[str] = set()
    pending = [scenario.entry_node]
    while pending:
        current = pending.pop()
        if current in seen or current not in ids:
            continue
        seen.add(current)
        node = scenario.node(current)
        if node is None:
            continue
        if node.kind is NodeKind.HANDOFF:
            return True
        pending.extend(t for t, _ in _targets(node) if t)
    # 폴백이 handoff면 어디서 막혀도 빠져나갈 수 있다.
    fallback = scenario.node(scenario.fallback_node) if scenario.fallback_node else None
    return bool(fallback and fallback.kind is NodeKind.HANDOFF)


def match_intent(node: DialogNode, text: str) -> Intent | None:
    """발화를 인텐트에 맞춘다. 먼저 선언된 것이 이긴다 — 저작자가 순서로
    우선순위를 표현할 수 있어야 한다."""
    for intent in node.intents:
        for pattern in intent.patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return intent
    return None


class DialogEngine:
    """시나리오 진행기."""

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    def start(self, session_id: str, tenant_id: str) -> StepResult:
        state = DialogState(
            session_id=session_id,
            tenant_id=tenant_id,
            scenario_id=self._scenario.scenario_id,
            scenario_version=self._scenario.version,
            current_node=self._scenario.entry_node,
        )
        return self._advance(state, self._scenario.entry_node)

    def reply(self, state: DialogState, text: str) -> StepResult:
        """고객 발화를 받아 한 걸음 나아간다."""
        if state.finished:
            return StepResult(utterances=[], state=state)

        node = self._scenario.node(state.current_node)
        if node is None:
            return self._fallback(state, "현재 노드를 찾을 수 없다")

        if node.kind is not NodeKind.ASK:
            return self._advance(state, node.next_node)

        intent = match_intent(node, text)
        if intent is not None:
            state.retries = 0
            if node.slot:
                state.slots[node.slot] = text.strip()
            return self._advance(state, intent.next_node or node.next_node)

        # 못 알아들었다. 상한을 넘으면 사람에게 넘긴다 —
        # 무한 재질문이 이 영역 최악의 경험이다.
        state.retries += 1
        if state.retries > node.max_retries:
            log.info(
                "재질문 상한 초과 — 사람 연결",
                extra={"session_id": state.session_id, "node": node.node_id},
            )
            return self._fallback(state, "재질문 상한 초과")

        state.turn_index += 1
        return StepResult(
            utterances=[Utterance(node.node_id, node.reprompt or node.prompt, expects_reply=True)],
            state=state,
        )

    def timeout(self, state: DialogState) -> StepResult:
        """무응답. 재질문과 같은 상한을 쓴다 — 침묵도 '못 알아들음'이다."""
        node = self._scenario.node(state.current_node)
        if node is None or node.kind is not NodeKind.ASK:
            return StepResult(utterances=[], state=state)
        return self.reply(state, "\x00무응답\x00")

    def _advance(self, state: DialogState, node_id: str) -> StepResult:
        """``say``를 이어 붙이다가 ``ask``·``handoff``·``end``에서 멈춘다."""
        utterances: list[Utterance] = []
        current = node_id
        state.turn_index += 1

        for _ in range(MAX_CHAIN):
            if not current:
                state.finished = True
                break
            node = self._scenario.node(current)
            if node is None:
                return self._fallback(state, f"노드 '{current}'가 없다")

            state.current_node = node.node_id
            if node.kind is NodeKind.SAY:
                utterances.append(Utterance(node.node_id, node.prompt))
                current = node.next_node
                continue
            if node.kind is NodeKind.BRANCH:
                current = self._branch(node, state)
                continue
            if node.kind is NodeKind.ASK:
                utterances.append(Utterance(node.node_id, node.prompt, expects_reply=True))
                state.retries = 0
                break
            if node.kind is NodeKind.HANDOFF:
                utterances.append(Utterance(node.node_id, node.prompt, handoff=True))
                state.handed_off = True
                state.finished = True
                break
            utterances.append(Utterance(node.node_id, node.prompt, ends_call=True))
            state.finished = True
            break
        else:
            # 순환. 고객은 봇이 혼자 끝없이 말하는 것을 듣게 된다.
            log.error(
                "시나리오 순환 감지 — 사람 연결",
                extra={"session_id": state.session_id, "node": current},
            )
            return self._fallback(state, "노드 순환")

        return StepResult(utterances=utterances, state=state)

    def _branch(self, node: DialogNode, state: DialogState) -> str:
        haystack = " ".join(state.slots.values())
        intent = match_intent(node, haystack)
        return (intent.next_node if intent else "") or node.next_node

    def _fallback(self, state: DialogState, reason: str) -> StepResult:
        """막혔을 때 갈 곳. 폴백조차 없으면 마지막 안내를 하고 끊는다 —
        아무 말 없이 끊기는 것보다는 낫다."""
        target = self._scenario.node(self._scenario.fallback_node)
        state.retries = 0
        if target is None:
            state.finished = True
            state.handed_off = True
            return StepResult(
                utterances=[
                    Utterance(
                        "__fallback__",
                        "죄송합니다. 상담원에게 연결해 드리겠습니다.",
                        handoff=True,
                    )
                ],
                state=state,
            )
        log.info("폴백", extra={"session_id": state.session_id, "reason": reason})
        return self._advance(state, target.node_id)
