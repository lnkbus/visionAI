"""sLLM 프로바이더 추상화 — 온프렘/SaaS 하이브리드의 핵심 지점.

폐쇄망은 vLLM 로컬 서빙, SaaS는 상용 API를 쓰지만 호출부(TA-ASSIST, LLM-SUM)는
차이를 모른다. 환경 분기(`if is_onprem:`) 대신 어댑터 주입으로 해결한다.

**프로파일** 개념을 둔 이유: 같은 게이트웨이가 크기가 다른 모델을 나눠 서빙해야
한다. 질의 추출은 1.5~3B SLM으로 200ms 안에 끝내야 하고, 요약은 7~14B로
품질을 내야 한다. 호출자는 ``profile="slm"`` 처럼 용도만 말하고, 어느 모델이
붙었는지는 설정이 정한다.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from vai_contracts.retrieval import CompletionRequest, CompletionResponse

log = logging.getLogger(__name__)


class BaseLLMAdapter(ABC):
    name: str

    @abstractmethod
    async def initialize(self, config: dict[str, Any]) -> None: ...

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    async def close(self) -> None:
        return None


class EchoLLMAdapter(BaseLLMAdapter):
    """모델 없이 도는 결정적 어댑터 — CI·데모용.

    질의 추출 프로파일에서는 프롬프트 끝의 발화를 그대로 질의로 돌려준다.
    실제 SLM만큼 맥락을 요약하지는 못하지만, 파이프라인 배선과 지연 예산을
    GPU 없이 검증할 수 있다.
    """

    name = "echo"

    async def initialize(self, config: dict[str, Any]) -> None:
        return None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        started = time.perf_counter()
        text = request.prompt.strip().splitlines()[-1] if request.prompt.strip() else ""
        return CompletionResponse(
            text=text[: request.max_tokens * 4],
            model=f"echo:{request.profile}",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class OpenAICompatibleAdapter(BaseLLMAdapter):
    """vLLM·TGI 등 OpenAI 호환 엔드포인트.

    폐쇄망 기본 경로다. vLLM이 OpenAI 스키마를 그대로 노출하므로 어댑터 하나로
    로컬 모델 대부분을 덮는다.
    """

    name = "openai_compatible"

    def __init__(self) -> None:
        self._client: Any = None
        self._models: dict[str, str] = {}
        self._timeout = 10.0

    async def initialize(self, config: dict[str, Any]) -> None:
        import httpx

        base_url = str(config.get("base_url", "http://localhost:8000/v1")).rstrip("/")
        self._timeout = float(config.get("timeout", self._timeout))
        # 프로파일 → 모델명 매핑. 미지정 프로파일은 default로 떨어진다.
        self._models = {str(k): str(v) for k, v in dict(config.get("models", {})).items()}
        self._models.setdefault("default", str(config.get("model", "local-model")))
        headers = {"Authorization": f"Bearer {config['api_key']}"} if config.get("api_key") else {}
        self._client = httpx.AsyncClient(base_url=base_url, timeout=self._timeout, headers=headers)

    def _model_for(self, profile: str) -> str:
        return self._models.get(profile, self._models["default"])

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        started = time.perf_counter()
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        response = await self._client.post(
            "/chat/completions",
            json={
                "model": self._model_for(request.profile),
                "messages": messages,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            },
        )
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage", {})
        return CompletionResponse(
            text=payload["choices"][0]["message"]["content"].strip(),
            model=payload.get("model", self._model_for(request.profile)),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class AnthropicAdapter(BaseLLMAdapter):
    """SaaS 경로. 폐쇄망 배포에서는 절대 활성화되지 않는다(외부 호출 0건 원칙)."""

    name = "anthropic"

    def __init__(self) -> None:
        self._client: Any = None
        self._models: dict[str, str] = {}

    async def initialize(self, config: dict[str, Any]) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=config.get("api_key"))
        self._models = {str(k): str(v) for k, v in dict(config.get("models", {})).items()}
        # 경량 작업은 작은 모델로 내려 비용과 지연을 함께 줄인다.
        self._models.setdefault("slm", "claude-haiku-4-5-20251001")
        self._models.setdefault("default", "claude-sonnet-5")

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        started = time.perf_counter()
        message = await self._client.messages.create(
            model=self._models.get(request.profile, self._models["default"]),
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=request.system or None,
            messages=[{"role": "user", "content": request.prompt}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return CompletionResponse(
            text=text.strip(),
            model=message.model,
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


def _load_openai_compatible() -> BaseLLMAdapter:
    return OpenAICompatibleAdapter()


def _load_anthropic() -> BaseLLMAdapter:
    return AnthropicAdapter()


_REGISTRY: dict[str, Callable[[], BaseLLMAdapter]] = {
    "echo": EchoLLMAdapter,
    "openai_compatible": _load_openai_compatible,
    "anthropic": _load_anthropic,
}


def create_llm(name: str) -> BaseLLMAdapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"알 수 없는 LLM 어댑터 '{name}'. 사용 가능: {sorted(_REGISTRY)}"
        ) from None
