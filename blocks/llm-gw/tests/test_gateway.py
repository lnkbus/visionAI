"""LLM-GW 계약 테스트."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from vai_contracts.retrieval import CompletionRequest, CompletionResponse
from vai_llm_gw.adapters import BaseLLMAdapter, EchoLLMAdapter, create_llm
from vai_llm_gw.app import create_app
from vai_llm_gw.usage import MemoryUsageMeter


class BrokenAdapter(BaseLLMAdapter):
    name = "broken"

    async def initialize(self, config: dict[str, object]) -> None:
        return None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise RuntimeError("GPU OOM")


def client_for(adapter: BaseLLMAdapter) -> TestClient:
    # 계측기를 주입해 Redis 없이 돈다. 게이트웨이 계약을 검증하는 데
    # 인프라가 필요하면 그 테스트는 CI에서 흔들린다.
    return TestClient(create_app(adapter, MemoryUsageMeter()))


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


def test_usage_is_metered_per_tenant() -> None:
    """총합만으로는 누구에게 청구할지 알 수 없다.

    SaaS 과금과 온프렘 용량 감시가 같은 계측값을 쓰되, 테넌트가 갈려 있어야
    그 값이 청구서로 이어진다.
    """
    with client_for(EchoLLMAdapter()) as client:
        client.post("/internal/v1/complete", json={"tenant_id": "acme", "prompt": "하나"})
        client.post("/internal/v1/complete", json={"tenant_id": "acme", "prompt": "둘"})
        client.post("/internal/v1/complete", json={"tenant_id": "beta", "prompt": "셋"})

        usage = client.get("/internal/v1/usage").json()

    assert usage["totals"]["calls"] == 3
    assert usage["by_tenant"]["acme"]["calls"] == 2
    assert usage["by_tenant"]["beta"]["calls"] == 1


def test_usage_declares_whether_it_survives_restart() -> None:
    """durable=False인 숫자를 청구 근거로 쓰면 안 된다 — 그 사실을 숨기지 않는다."""
    with client_for(EchoLLMAdapter()) as client:
        usage = client.get("/internal/v1/usage").json()

    assert usage["durable"] is False, "Redis 없는 구성은 재기동으로 0이 된다"
    assert usage["month"]


def test_configured_timeout_reaches_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정을 바꿔도 아무 일이 없으면, 그건 없는 설정보다 나쁘다.

    ``VAI_LLM_REQUEST_TIMEOUT_S``는 오래 선언만 되어 있고 어댑터에 닿지 않았다.
    운영자가 값을 바꿔도 프로바이더 호출은 어댑터 기본값(10초)으로 나갔다.
    """
    monkeypatch.setenv("VAI_LLM_ADAPTER", "openai_compatible")
    monkeypatch.setenv("VAI_LLM_REQUEST_TIMEOUT_S", "3.5")
    monkeypatch.setenv("VAI_LLM_ADAPTER_CONFIG", '{"base_url": "http://localhost:9/v1"}')

    with TestClient(create_app(meter=MemoryUsageMeter())) as client:
        assert client.app.state.llm._client.timeout.read == 3.5


def test_explicit_adapter_timeout_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """어댑터 설정에 명시했으면 그쪽이 이긴다 — 세밀한 설정이 뭉뚱그린 값에 덮이면 안 된다."""
    monkeypatch.setenv("VAI_LLM_ADAPTER", "openai_compatible")
    monkeypatch.setenv("VAI_LLM_REQUEST_TIMEOUT_S", "3.5")
    monkeypatch.setenv(
        "VAI_LLM_ADAPTER_CONFIG", '{"base_url": "http://localhost:9/v1", "timeout": 30}'
    )

    with TestClient(create_app(meter=MemoryUsageMeter())) as client:
        assert client.app.state.llm._client.timeout.read == 30


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


# ── 사용량 계측 ──────────────────────────────────────────────────────────────


async def test_redis_meter_survives_restart_and_replicas() -> None:
    """복제본이 각자 다른 숫자를 들고 있으면 그건 사용량이 아니다."""
    import redis.asyncio as aioredis

    from vai_llm_gw.usage import RedisUsageMeter, period

    client = aioredis.Redis(db=15)
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis가 없다")

    try:
        async for key in client.scan_iter(match="vai:llm-usage:*"):
            await client.delete(key)

        # 서로 다른 프로세스를 흉내 낸다 — 같은 Redis를 보는 두 게이트웨이.
        first = RedisUsageMeter(client)
        second = RedisUsageMeter(aioredis.Redis(db=15))
        await first.record("acme", prompt_tokens=10, completion_tokens=5)
        await second.record("acme", prompt_tokens=3, completion_tokens=2)

        # 재기동을 흉내 낸다 — 새 인스턴스가 이전 값을 이어 본다.
        snapshot = await RedisUsageMeter(client).snapshot(period())

        assert snapshot["acme"]["calls"] == 2
        assert snapshot["acme"]["prompt_tokens"] == 13
    finally:
        async for key in client.scan_iter(match="vai:llm-usage:*"):
            await client.delete(key)
        await client.aclose()


async def test_recording_failure_does_not_break_the_call() -> None:
    """계측이 상담을 멈추면 안 된다. 다만 조용히 넘어가지도 않는다."""
    import redis.asyncio as aioredis

    from vai_llm_gw.usage import RedisUsageMeter

    unreachable = RedisUsageMeter(aioredis.Redis(host="127.0.0.1", port=1, socket_timeout=0.1))

    await unreachable.record("acme", prompt_tokens=1, completion_tokens=1)  # 예외가 나오면 실패


def test_usage_reports_unreadable_backend_instead_of_zero() -> None:
    """읽을 수 없는 것을 '사용량 0'으로 답하면 청구가 조용히 빈다."""

    class BrokenMeter(MemoryUsageMeter):
        async def snapshot(self, month: str = "") -> dict[str, dict[str, int]]:
            raise RuntimeError("저장소 불통")

    with TestClient(create_app(EchoLLMAdapter(), BrokenMeter())) as client:
        assert client.get("/internal/v1/usage").status_code == 503


def test_months_do_not_bleed_into_each_other() -> None:
    """지난달 사용량이 이번 달 청구에 섞이면 안 된다."""
    from vai_llm_gw.usage import period

    with client_for(EchoLLMAdapter()) as client:
        client.post("/internal/v1/complete", json={"tenant_id": "acme", "prompt": "하나"})

        current = client.get("/internal/v1/usage").json()
        past = client.get("/internal/v1/usage", params={"month": "2020-01"}).json()

    assert current["month"] == period()
    assert current["totals"]["calls"] == 1
    assert past["totals"]["calls"] == 0
