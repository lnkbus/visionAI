"""LLM-GW 계약 테스트."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_contracts.retrieval import CompletionRequest, CompletionResponse
from vai_llm_gw.adapters import BaseLLMAdapter, EchoLLMAdapter, create_llm
from vai_llm_gw.app import create_app


class BrokenAdapter(BaseLLMAdapter):
    name = "broken"

    async def initialize(self, config: dict[str, object]) -> None:
        return None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise RuntimeError("GPU OOM")


def client_for(adapter: BaseLLMAdapter) -> TestClient:
    return TestClient(create_app(adapter))


def test_registry_lists_all_deployment_targets() -> None:
    """온프렘(vLLM)·SaaS(상용 API)·CI(echo)가 모두 같은 인터페이스 뒤에 있어야 한다."""
    for name in ("echo", "openai_compatible", "anthropic"):
        assert create_llm(name).name


def test_registry_rejects_unknown_adapter() -> None:
    with pytest.raises(ValueError, match="echo"):
        create_llm("nope")


def test_completion_returns_text() -> None:
    with client_for(EchoLLMAdapter()) as client:
        response = client.post(
            "/internal/v1/complete",
            json={"tenant_id": "t1", "prompt": "고객: 결제일 연기 되나요?", "profile": "slm"},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "고객: 결제일 연기 되나요?"


def test_backend_failure_is_reported_as_unavailable_not_error() -> None:
    """500으로 흘리면 호출부가 재시도 폭풍을 만든다. 503이어야 폴백을 탄다."""
    with client_for(BrokenAdapter()) as client:
        response = client.post(
            "/internal/v1/complete", json={"tenant_id": "t1", "prompt": "무엇이든"}
        )

    assert response.status_code == 503


def test_usage_is_metered_for_billing() -> None:
    """SaaS 과금과 온프렘 용량 감시가 같은 계측값을 쓴다."""
    with client_for(EchoLLMAdapter()) as client:
        client.post("/internal/v1/complete", json={"tenant_id": "t1", "prompt": "하나"})
        client.post("/internal/v1/complete", json={"tenant_id": "t1", "prompt": "둘"})

        usage = client.get("/internal/v1/usage").json()

    assert usage["calls"] == 2


async def test_openai_adapter_maps_profiles_to_models() -> None:
    """질의 추출(SLM)과 요약(대형)이 같은 게이트웨이에서 다른 모델로 나뉜다."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": captured["model"],
                "choices": [{"message": {"content": "응답"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    from vai_llm_gw.adapters import OpenAICompatibleAdapter

    adapter = OpenAICompatibleAdapter()
    await adapter.initialize(
        {"base_url": "http://vllm/v1", "models": {"slm": "qwen-1.5b", "default": "qwen-14b"}}
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://vllm/v1"
    )

    result = await adapter.complete(CompletionRequest(tenant_id="t1", prompt="질문", profile="slm"))

    assert captured["model"] == "qwen-1.5b"
    assert result.text == "응답"
    assert result.prompt_tokens == 5


async def test_unknown_profile_falls_back_to_default_model() -> None:
    from vai_llm_gw.adapters import OpenAICompatibleAdapter

    adapter = OpenAICompatibleAdapter()
    await adapter.initialize({"models": {"default": "qwen-14b"}})

    assert adapter._model_for("nonexistent") == "qwen-14b"
