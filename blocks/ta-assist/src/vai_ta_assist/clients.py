"""TA-ASSIST가 의존하는 다른 블록의 클라이언트.

블록 간 통신은 계약(HTTP)으로만 한다는 규칙 때문에 RAG-SRCH와 LLM-GW를
직접 import 하지 않고 얇은 클라이언트를 둔다.
"""

from __future__ import annotations

import logging

import httpx

from vai_contracts.retrieval import (
    CompletionRequest,
    CompletionResponse,
    SearchRequest,
    SearchResponse,
)

log = logging.getLogger(__name__)


class SearchClient:
    """RAG-SRCH 호출. 실시간 경로라 타임아웃이 짧다."""

    def __init__(
        self, base_url: str, *, timeout: float = 0.5, client: httpx.AsyncClient | None = None
    ) -> None:
        # 검색 예산이 100ms이므로 500ms면 이미 명백한 장애다. 더 기다려 봐야
        # 상담원은 그 팝업을 안 본다.
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def search(self, request: SearchRequest) -> SearchResponse:
        try:
            response = await self._client.post(
                "/internal/v1/search", json=request.model_dump(mode="json")
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # 검색 실패로 상담 파이프라인을 막지 않는다. 팝업만 안 뜬다.
            log.warning("검색 호출 실패", exc_info=True)
            return SearchResponse()
        return SearchResponse.model_validate(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()


class LlmClient:
    """LLM-GW 호출. 질의 추출 프로파일(SLM)을 쓴다."""

    def __init__(
        self,
        base_url: str,
        tenant_id: str = "",
        *,
        timeout: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._tenant_id = tenant_id

    async def complete(self, prompt: str, system: str = "") -> str:
        """:class:`QueryExtractor`가 기대하는 호출 형태."""
        response = await self._client.post(
            "/internal/v1/complete",
            json=CompletionRequest(
                tenant_id=self._tenant_id,
                prompt=prompt,
                system=system,
                profile="slm",
                max_tokens=48,
            ).model_dump(mode="json"),
        )
        response.raise_for_status()
        return CompletionResponse.model_validate(response.json()).text

    async def aclose(self) -> None:
        await self._client.aclose()
