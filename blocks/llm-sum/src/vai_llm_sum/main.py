"""LLM-SUM 진입점 — 워커 2개 + 요약 조회 API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import RedisEventBus, build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_contracts.retrieval import CompletionRequest, CompletionResponse
from vai_contracts.summary import Summary
from vai_llm_sum.store import InMemorySummaryStore, RedisSummaryStore
from vai_llm_sum.worker import (
    BLOCK_ID,
    SpeakerLabelCollector,
    SummaryWorker,
    TranscriptBuffer,
    TranscriptCollector,
)

log = logging.getLogger(__name__)


class SumSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_SUM_", extra="ignore")

    llm_url: str = "http://localhost:8085"
    timeout_s: float = 60.0
    """요약은 배치라 넉넉히 준다. 실시간 경로의 0.5초와는 성격이 다르다."""


class LlmSumClient:
    """LLM-GW 호출. 요약 프로파일(대형 모델)을 쓴다."""

    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def __call__(self, *, prompt: str, system: str, tenant_id: str, max_tokens: int) -> str:
        response = await self._client.post(
            "/internal/v1/complete",
            json=CompletionRequest(
                tenant_id=tenant_id,
                prompt=prompt,
                system=system,
                profile="default",
                max_tokens=max_tokens,
            ).model_dump(mode="json"),
        )
        response.raise_for_status()
        return CompletionResponse.model_validate(response.json()).text

    async def aclose(self) -> None:
        await self._client.aclose()


def create_app() -> FastAPI:
    common = get_settings()
    cfg = SumSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        store = (
            RedisSummaryStore(bus.redis)
            if isinstance(bus, RedisEventBus)
            else InMemorySummaryStore()
        )
        buffer = TranscriptBuffer()
        llm = LlmSumClient(cfg.llm_url, cfg.timeout_s)

        collector = TranscriptCollector(
            bus, buffer, group=f"{common.consumer_group}:transcript", consumer=common.consumer_name
        )
        summarizer = SummaryWorker(
            bus,
            buffer,
            llm,
            store,
            group=f"{common.consumer_group}:summary",
            consumer=common.consumer_name,
        )
        application.state.store = store
        # 회의 프로파일에서 화자 라벨을 녹취록에 붙인다. 라벨이 없어도
        # (AICC이거나 SPK-DIA 미배포) 요약은 채널 이름으로 만들어진다.
        labels = SpeakerLabelCollector(
            bus, buffer, group=f"{common.consumer_group}:labels", consumer=common.consumer_name
        )
        tasks = [
            asyncio.create_task(collector.run(), name="sum-collector"),
            asyncio.create_task(labels.run(), name="sum-labels"),
            asyncio.create_task(summarizer.run(), name="sum-worker"),
        ]
        try:
            yield
        finally:
            collector.stop()
            labels.stop()
            summarizer.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await llm.aclose()
            await bus.close()

    app = create_block_app(
        block_id=BLOCK_ID, title="VisionAI LLM-SUM", settings=common, lifespan=lifespan
    )

    @app.get("/internal/v1/summaries/{session_id}", response_model=Summary, tags=["summary"])
    async def get_summary(session_id: str, request: Request) -> Summary:
        summary: Summary | None = await request.app.state.store.get(session_id)
        if summary is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="요약을 찾을 수 없다")
        return summary

    @app.get("/internal/v1/summaries", response_model=list[Summary], tags=["summary"])
    async def list_summaries(tenant_id: str, request: Request, limit: int = 50) -> list[Summary]:
        summaries: list[Summary] = await request.app.state.store.list_for(tenant_id, limit)
        return summaries

    @app.put("/internal/v1/summaries/{session_id}", response_model=Summary, tags=["summary"])
    async def update_summary(session_id: str, payload: Summary, request: Request) -> Summary:
        """사람이 수정한 요약을 저장한다.

        ``edited`` 플래그가 곧 품질 지표다 — 수정률이 높으면 프롬프트나 모델을
        손봐야 한다는 신호다(docs/06 §5).
        """
        existing: Summary | None = await request.app.state.store.get(session_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="요약을 찾을 수 없다")
        payload.session_id = session_id
        payload.tenant_id = existing.tenant_id
        payload.edited = True
        await request.app.state.store.put(payload)
        return payload

    return app


app = create_app()


def main() -> None:
    serve(app, default_port=8089)


if __name__ == "__main__":
    main()
