"""BOT-VOICE가 의존하는 다른 블록의 클라이언트.

블록 간 통신은 계약(HTTP)으로만 한다는 규칙 때문에 LLM-GW를 직접 import 하지
않고 얇은 클라이언트를 둔다.
"""

from __future__ import annotations

import httpx

from vai_contracts.retrieval import CompletionRequest, CompletionResponse


class LlmClient:
    """LLM-GW 호출. 의도 분류 프로파일(SLM)을 쓴다.

    타임아웃이 짧다. 통화 중이라 늦은 정답은 정답이 아니고, 워커가 다시 한 번
    상한을 걸어 되묻기로 흡수한다.
    """

    def __init__(
        self,
        base_url: str,
        tenant_id: str = "",
        *,
        timeout: float = 0.6,
        profile: str = "slm",
        max_tokens: int = 8,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._tenant_id = tenant_id
        self._profile = profile
        self._max_tokens = max_tokens
        """인텐트 이름 하나만 나오면 된다. 길게 허용하면 모델이 설명을 붙인다."""

    async def complete(self, prompt: str, system: str = "") -> str:
        response = await self._client.post(
            "/internal/v1/complete",
            json=CompletionRequest(
                tenant_id=self._tenant_id,
                prompt=prompt,
                system=system,
                profile=self._profile,
                max_tokens=self._max_tokens,
            ).model_dump(mode="json"),
        )
        response.raise_for_status()
        return CompletionResponse.model_validate(response.json()).text

    async def aclose(self) -> None:
        await self._client.aclose()
