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
