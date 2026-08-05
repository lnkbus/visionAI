"""TA-ASSIST가 의존하는 바깥 능력의 타입 정의.

블록 간 결합을 계약으로만 하려면 워커가 "무엇을 호출하는지"만 알고 "누가
제공하는지"는 몰라야 한다. 프로토콜로 그 경계를 타입 수준에서 고정한다.
덕분에 테스트는 HTTP 클라이언트 대신 검색 엔진을 직접 꽂을 수 있다.
"""

from __future__ import annotations

from typing import Protocol

from vai_contracts.retrieval import SearchRequest, SearchResponse


class SearchPort(Protocol):
    """RAG-SRCH 호출 능력."""

    async def __call__(self, request: SearchRequest) -> SearchResponse: ...


class CompletePort(Protocol):
    """LLM-GW 호출 능력 (질의 추출용)."""

    async def __call__(self, prompt: str, system: str = "") -> str: ...
