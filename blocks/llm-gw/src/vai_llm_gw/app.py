"""LLM-GW 내부 API.

호출부가 프로바이더를 직접 부르지 않고 이 게이트웨이를 거치게 하는 이유:

* **폐쇄망 보증** — 외부 호출 지점이 한 군데로 모이면 "외부 통신 0건"을
  네트워크 정책으로 증명하기 쉽다.
* **사용량 계측** — 토큰 집계가 한곳에서 이뤄져 SaaS 과금·온프렘 용량 관리에
  같은 코드를 쓴다.
* **폴백** — 모델 장애 시 축소 응답이나 상담원 전환으로 떨어뜨리는 정책을
  블록마다 중복 구현하지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import RedisEventBus, build_bus
from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.retrieval import CompletionRequest, CompletionResponse
from vai_llm_gw.adapters import BaseLLMAdapter, create_llm
from vai_llm_gw.usage import MemoryUsageMeter, RedisUsageMeter, UsageMeter, period

log = logging.getLogger(__name__)
BLOCK_ID = "LLM-GW"


class LlmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_LLM_", extra="ignore")

    adapter: str = "echo"
    """운영 기본값은 openai_compatible(vLLM). GPU 없는 개발/CI를 위해 echo로 둔다."""

    adapter_config: str = "{}"

    request_timeout_s: float = 10.0
    """프로바이더 호출 타임아웃.

    ``adapter_config``에 ``timeout``을 명시하면 그쪽이 이긴다. 이 값이 있는데도
    어댑터가 자기 기본값을 쓰면, 운영자가 설정을 바꿔도 아무 일이 일어나지
    않는다 — 조용히 무시되는 설정이 가장 오래 사람을 헤매게 한다."""


def create_app(adapter: BaseLLMAdapter | None = None, meter: UsageMeter | None = None) -> FastAPI:
    common = get_settings()
    llm_cfg = LlmSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        import json

        engine = adapter or create_llm(llm_cfg.adapter)
        if adapter is None:
            adapter_config = json.loads(llm_cfg.adapter_config)
            adapter_config.setdefault("timeout", llm_cfg.request_timeout_s)
            await engine.initialize(adapter_config)
        application.state.llm = engine

        # 계측 백엔드를 주입받으면 버스를 열지 않는다. 게이트웨이가 토큰을
        # 세기 위해 Redis 연결을 요구하면, 테스트와 단일 프로세스 구성이
        # 인프라에 묶인다.
        bus = None if meter is not None else build_bus(common.redis_url)
        active_meter: UsageMeter = meter or (
            RedisUsageMeter(bus.redis) if isinstance(bus, RedisEventBus) else MemoryUsageMeter()
        )
        application.state.meter = active_meter
        log.info(
            "LLM 어댑터 로드",
            extra={"adapter": engine.name, "usage_durable": active_meter.durable},
        )
        try:
            yield
        finally:
            if adapter is None:
                await engine.close()
            if bus is not None:
                await bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI LLM-GW", settings=common, lifespan=lifespan
    )

    @app.post("/internal/v1/complete", response_model=CompletionResponse, tags=["llm"])
    async def complete(payload: CompletionRequest, request: Request) -> CompletionResponse:
        engine: BaseLLMAdapter = request.app.state.llm
        try:
            result = await engine.complete(payload)
        except Exception as exc:
            # 모델 장애를 500으로 흘리면 호출부가 재시도 폭풍을 만든다.
            # 503으로 명확히 알려 상담원 전환 같은 폴백을 타게 한다.
            log.exception("LLM 호출 실패", extra={"tenant_id": payload.tenant_id})
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="LLM 백엔드를 사용할 수 없다"
            ) from exc

        await request.app.state.meter.record(
            payload.tenant_id,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result

    @app.get("/internal/v1/usage", tags=["llm"])
    async def usage(request: Request, month: str = "") -> dict[str, object]:
        """테넌트별 토큰 사용량. SaaS 과금·온프렘 용량 감시의 입력.

        ``durable``이 False면 이 숫자는 **이 프로세스가 뜬 뒤의 합**일 뿐이다.
        재기동으로 0이 되고 복제본마다 다르다 — 청구 근거로 쓸 수 없다는 뜻이라
        숨기지 않고 함께 돌려준다.
        """
        active: UsageMeter = request.app.state.meter
        try:
            by_tenant = await active.snapshot(month)
        except Exception as exc:
            # 계측 저장소를 못 읽는 것을 "사용량 0"으로 답하면 청구가 조용히
            # 빈다. 읽을 수 없다는 사실을 그대로 알린다.
            log.exception("사용량 조회 실패")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="사용량 저장소를 읽을 수 없다"
            ) from exc
        totals = {
            field: sum(values.get(field, 0) for values in by_tenant.values())
            for field in ("calls", "prompt_tokens", "completion_tokens")
        }
        return {
            "month": month or period(),
            "durable": active.durable,
            "totals": totals,
            "by_tenant": by_tenant,
        }

    return app
