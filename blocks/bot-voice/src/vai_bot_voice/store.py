"""대화 상태 저장소.

**상태를 봇 프로세스 안에 두지 않는다.** 통화 중에 봇이 재기동되면 그 통화가
끝나는 설계는 운영할 수 없다. 배포 한 번에 진행 중인 통화가 전부 끊긴다.

시나리오도 여기서 읽는다. 저작 도구(SCN-STUDIO)가 쓰고 봇은 읽기만 한다 —
저작 도구가 꺼져 있어도 마지막 배포본으로 통화가 계속 돈다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from redis.asyncio import Redis

from vai_contracts.dialog import DialogState, Scenario

log = logging.getLogger(__name__)

STATE_PREFIX = "vai:bot:state:"
SCENARIO_PREFIX = "vai:bot:scenario:"
STATE_TTL = 60 * 60 * 4
"""통화 하나의 최대 길이를 넉넉히 덮는다. 종료 이벤트를 놓쳐도 무한정 쌓이지 않는다."""


class DialogStore(ABC):
    @abstractmethod
    async def get_state(self, session_id: str) -> DialogState | None: ...

    @abstractmethod
    async def save_state(self, state: DialogState) -> None: ...

    @abstractmethod
    async def clear_state(self, session_id: str) -> None: ...

    @abstractmethod
    async def get_scenario(self, tenant_id: str) -> Scenario | None: ...

    @abstractmethod
    async def put_scenario(self, scenario: Scenario) -> None: ...


class RedisDialogStore(DialogStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get_state(self, session_id: str) -> DialogState | None:
        raw = await self._redis.get(STATE_PREFIX + session_id)
        return DialogState.model_validate_json(raw) if raw else None

    async def save_state(self, state: DialogState) -> None:
        await self._redis.set(
            STATE_PREFIX + state.session_id, state.model_dump_json().encode(), ex=STATE_TTL
        )

    async def clear_state(self, session_id: str) -> None:
        await self._redis.delete(STATE_PREFIX + session_id)

    async def get_scenario(self, tenant_id: str) -> Scenario | None:
        raw = await self._redis.get(SCENARIO_PREFIX + tenant_id)
        return Scenario.model_validate_json(raw) if raw else None

    async def put_scenario(self, scenario: Scenario) -> None:
        await self._redis.set(
            SCENARIO_PREFIX + scenario.tenant_id, scenario.model_dump_json().encode()
        )


class InMemoryDialogStore(DialogStore):
    """테스트·단일 프로세스 실행용."""

    def __init__(self) -> None:
        self.states: dict[str, DialogState] = {}
        self.scenarios: dict[str, Scenario] = {}

    async def get_state(self, session_id: str) -> DialogState | None:
        return self.states.get(session_id)

    async def save_state(self, state: DialogState) -> None:
        self.states[state.session_id] = state

    async def clear_state(self, session_id: str) -> None:
        self.states.pop(session_id, None)

    async def get_scenario(self, tenant_id: str) -> Scenario | None:
        return self.scenarios.get(tenant_id)

    async def put_scenario(self, scenario: Scenario) -> None:
        self.scenarios[scenario.tenant_id] = scenario
