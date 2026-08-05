"""LLM-SUM이 의존하는 바깥 능력."""

from __future__ import annotations

from typing import Protocol

from vai_contracts.summary import Summary


class CompletePort(Protocol):
    """LLM-GW 호출 능력."""

    async def __call__(
        self, *, prompt: str, system: str, tenant_id: str, max_tokens: int
    ) -> str: ...


class SummaryStore(Protocol):
    """요약 저장소. Wave 6에서 PostgreSQL로 교체된다."""

    async def put(self, summary: Summary) -> None: ...

    async def get(self, session_id: str) -> Summary | None: ...

    async def list_for(self, tenant_id: str, limit: int = 50) -> list[Summary]: ...
