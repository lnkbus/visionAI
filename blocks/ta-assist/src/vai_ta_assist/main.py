"""TA-ASSIST 진입점."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from vai_common.bus import build_bus
from vai_common.service import create_block_app, serve
from vai_common.settings import get_settings
from vai_ta_assist.clients import LlmClient, SearchClient
from vai_ta_assist.extractor import QueryExtractor
from vai_ta_assist.worker import BLOCK_ID, AssistWorker

log = logging.getLogger(__name__)


class AssistSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VAI_ASSIST_", extra="ignore")

    search_url: str = "http://localhost:8087"
    llm_url: str = "http://localhost:8085"
    use_slm: bool = True
    """False면 SLM 호출 없이 어휘 폴백만 쓴다. GPU가 부족한 소형 프로파일에서
    팝업을 포기하지 않고 품질만 낮춰 운영하는 경로."""

    kb_id: str = "default"
    top_k: int = 3
    min_score: float = 0.0
    workers: int = 2
    """검색·SLM 대기가 있어 워커 하나면 큐가 밀린다."""


def create_app() -> FastAPI:
    common = get_settings()
    cfg = AssistSettings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        bus = build_bus(common.redis_url)
        search = SearchClient(cfg.search_url)
        llm = LlmClient(cfg.llm_url) if cfg.use_slm else None
        extractor = QueryExtractor(llm.complete if llm else None)

        workers = [
            AssistWorker(
                bus,
                extractor,
                search.search,
                group=common.consumer_group,
                consumer=f"{common.consumer_name}-{i}",
                kb_id=cfg.kb_id,
                top_k=cfg.top_k,
                min_score=cfg.min_score,
            )
            for i in range(max(1, cfg.workers))
        ]
        application.state.workers = workers
        tasks = [
            asyncio.create_task(w.run(), name=f"assist-worker-{i}") for i, w in enumerate(workers)
        ]
        log.info(
            "Agent Assist 기동",
            extra={"kb_id": cfg.kb_id, "slm": cfg.use_slm, "workers": len(workers)},
        )
        try:
            yield
        finally:
            for worker in workers:
                worker.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await search.aclose()
            if llm is not None:
                await llm.aclose()
            await bus.close()

    return create_block_app(
        block_id=BLOCK_ID, title="VisionAI TA-ASSIST", settings=common, lifespan=lifespan
    )


app = create_app()


def main() -> None:
    serve(app, default_port=8088)


if __name__ == "__main__":
    main()
