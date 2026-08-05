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
    """요약 저장소.

    구현은 둘이다. 파일 저장소가 기본이고(재기동·TTL과 무관하게 남는다),
    Redis 저장소는 요약을 상담 이력 시스템으로 곧바로 넘기는 구성에서 쓴다 —
    그쪽이 영속 보관을 책임진다면 여기 남길 이유가 없다.
    """

    async def put(self, summary: Summary) -> None: ...

    async def get(self, session_id: str) -> Summary | None: ...

    async def list_for(self, tenant_id: str, limit: int = 50) -> list[Summary]: ...

    async def delete(self, session_id: str) -> bool:
        """요약 1건 파기. 개인정보 파기 요구에 응할 수 있어야 한다(docs/05 §2.2)."""
        ...
