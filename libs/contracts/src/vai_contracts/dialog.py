"""대화 계약 — 음성봇 시나리오와 턴 관리.

**시나리오는 코드가 아니라 데이터다.** 고객사가 안내 문구 한 줄 바꾸는 데
공급사 배포가 필요하면 유지보수 원가가 라이선스 수익을 잠식한다. 흐름을
저작 도구가 편집할 수 있는 형태로 둔다.

**대화 상태는 세션에 있고 봇에는 없다.** 봇 프로세스가 죽어도 통화가 이어지려면
상태가 밖에 있어야 한다 — 통화 중 재기동이 곧 통화 종료인 설계는 운영할 수 없다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from vai_contracts.events import BaseEvent


class NodeKind(StrEnum):
    SAY = "say"
    """안내만 하고 다음으로 넘어간다."""

    ASK = "ask"
    """묻고 답을 기다린다. 무응답·오인식 처리가 여기 붙는다."""

    BRANCH = "branch"
    """수집한 값으로 갈래를 고른다."""

    HANDOFF = "handoff"
    """사람 상담원 연결. **모든 시나리오에 반드시 도달 가능해야 한다** —
    빠져나갈 곳 없는 봇은 고객을 가둔다."""

    END = "end"


class Intent(BaseModel):
    """발화를 갈래로 잇는 규칙."""

    name: str
    patterns: list[str] = Field(default_factory=list)
    """정규식. 저작 도구가 즉시 시험할 수 있어야 한다."""

    next_node: str = ""

    description: str = ""
    """이 갈래가 무슨 뜻인지 사람 말로. 분류기가 읽는다.

    정규식은 '어떤 글자가 오면'을 말할 뿐 '무슨 뜻인지'는 말하지 않는다.
    ``confirm``이라는 이름만 보고 모델이 알아서 맞히기를 기대하면, 저작자가
    의도한 것과 다른 기준으로 분류된다."""

    examples: list[str] = Field(default_factory=list)
    """이 갈래에 해당하는 실제 발화 예시.

    정규식을 늘리는 것보다 예시를 늘리는 편이 저작자에게 쉽다 — "네|예|응"을
    계속 이어 붙이는 대신 고객이 실제로 한 말을 옮겨 적으면 된다. 어휘 분류기가
    바로 쓰고, sLLM 분류기는 프롬프트에 싣는다."""


class DialogNode(BaseModel):
    """시나리오의 한 마디."""

    node_id: str
    kind: NodeKind
    prompt: str = ""
    """봇이 할 말. TTS 전처리를 거치므로 숫자를 그대로 써도 된다."""

    next_node: str = ""
    intents: list[Intent] = Field(default_factory=list)

    slot: str = ""
    """``ask``가 채우는 값의 이름(예: ``birth_date``)."""

    reprompt: str = ""
    """못 알아들었을 때 다시 묻는 말. 같은 문장을 반복하면 고객은 봇이
    고장 났다고 여긴다 — 다르게 묻는 것이 중요하다."""

    max_retries: int = 2
    """이 횟수를 넘기면 사람에게 넘긴다. 무한 재질문은 최악의 경험이다."""

    timeout_ms: int = 6000
    """무응답 대기. 너무 짧으면 생각하는 고객을 끊고, 길면 통화가 멈춘 것처럼 들린다."""


class Scenario(BaseModel):
    """시나리오 한 벌."""

    scenario_id: str
    tenant_id: str
    version: int = 1
    entry_node: str = "start"
    nodes: list[DialogNode] = Field(default_factory=list)
    fallback_node: str = ""
    """어디서든 막히면 갈 곳. 보통 ``handoff``다."""

    published: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def node(self, node_id: str) -> DialogNode | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)


class ScenarioStage(StrEnum):
    """시나리오 생애주기.

    한 칸씩만 올라간다. 편집 직후 바로 운영에 올리는 길을 열어 두면 결국
    그 길로만 다니게 되고, 검증과 시연은 형식이 된다.

    **편집하면 무조건 DRAFT로 내려간다.** 검증을 통과한 뒤 한 글자만 고쳐도
    그 시나리오는 더 이상 검증된 물건이 아니다.
    """

    DRAFT = "draft"
    """편집 중. 아직 아무것도 보장하지 않는다."""

    BUILT = "built"
    """구조 검증 통과 — 막다른 골목이 없고 사람 연결에 도달할 수 있다."""

    STAGING = "staging"
    """시뮬레이터로 실제 대화를 돌려 봤다. 구조가 맞는 것과 말이 되는 것은 다르다."""

    LIVE = "live"
    """운영 배포됨. 지금 고객이 이 흐름을 듣고 있다."""


STAGE_ORDER = [ScenarioStage.DRAFT, ScenarioStage.BUILT, ScenarioStage.STAGING, ScenarioStage.LIVE]


class ScenarioRevision(BaseModel):
    """시나리오 한 판 + 그것이 어디까지 왔는지.

    시나리오 본문과 생애주기를 한 덩어리로 둔다. 따로 두면 "어느 버전이
    운영인가"를 두 곳에서 물어야 하고, 두 답이 갈라지는 순간 아무도 못 믿는다.
    """

    scenario: Scenario
    stage: ScenarioStage = ScenarioStage.DRAFT
    note: str = ""
    """무엇을 왜 바꿨는가. 되돌릴 때 이 줄만 보고 고른다."""

    updated_by: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    problems: list[str] = Field(default_factory=list)
    """마지막 검증에서 나온 문제. 비어 있다고 검증했다는 뜻은 아니다 —
    ``stage``가 그것을 말한다."""


class DialogState(BaseModel):
    """세션 하나의 대화 상태. 봇 프로세스 밖에 저장한다."""

    session_id: str
    tenant_id: str
    scenario_id: str
    scenario_version: int = 1
    current_node: str = ""
    slots: dict[str, str] = Field(default_factory=dict)
    retries: int = 0
    turn_index: int = 0
    finished: bool = False
    handed_off: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def turn_id(self) -> str:
        """턴 식별자. 끼어들기가 이 단위로 취소한다."""
        return f"{self.session_id}:{self.turn_index}"


class BotTurn(BaseEvent):
    """``bot.turn`` — 봇이 한 마디 했다는 기록.

    상담원 화면·회의록·감사가 모두 이걸 본다. 봇이 무슨 말을 했는지 남지 않으면
    분쟁이 생겼을 때 아무것도 증명할 수 없다.
    """

    turn_id: str
    node_id: str
    text: str
    slots: dict[str, str] = Field(default_factory=dict)
    handed_off: bool = False
