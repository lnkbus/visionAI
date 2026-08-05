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

from vai_common.service import create_block_app
from vai_common.settings import get_settings
from vai_contracts.retrieval import CompletionRequest, CompletionResponse
from vai_llm_gw.adapters import BaseLLMAdapter, create_llm

log = logging.getLogger(__name__)
BLOCK_ID = "LLM-GW"


class LlmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_LLM_", extra="ignore")

    adapter: str = "echo"
    """운영 기본값은 openai_compatible(vLLM). GPU 없는 개발/CI를 위해 echo로 둔다."""

    adapter_config: str = "{}"
    request_timeout_s: float = 10.0


def create_app(adapter: BaseLLMAdapter | None = None) -> FastAPI:
    common = get_settings()
    llm_cfg = LlmSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        import json

        engine = adapter or create_llm(llm_cfg.adapter)
        if adapter is None:
            await engine.initialize(json.loads(llm_cfg.adapter_config))
        application.state.llm = engine
        application.state.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        log.info("LLM 어댑터 로드", extra={"adapter": engine.name})
        try:
            yield
        finally:
            if adapter is None:
                await engine.close()

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

        usage = request.app.state.usage
        usage["calls"] += 1
        usage["prompt_tokens"] += result.prompt_tokens
        usage["completion_tokens"] += result.completion_tokens
        return result

    @app.get("/internal/v1/usage", tags=["llm"])
    async def usage(request: Request) -> dict[str, int]:
        """토큰 사용량 스냅샷. SaaS 과금·온프렘 용량 감시의 입력."""
        return dict(request.app.state.usage)

    return app
