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
from vai_ta_assist.answer import AnswerComposer
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

    generate_answers: bool = True
    """추천 답변 생성. 끄면 근거 원문만 뜬다.

    GPU가 빠듯한 소형 프로파일에서 팝업 자체를 포기하지 않고 생성만 내리는
    경로다. 켜 두어도 검증을 통과하지 못한 답변은 표시되지 않는다."""

    answer_profile: str = "answer"
    """LLM-GW 프로파일. 질의 추출용 SLM보다 큰 모델을 가리키도록 설정한다."""

    answer_timeout: float = 2.5
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
        answer_llm = (
            LlmClient(
                cfg.llm_url,
                timeout=cfg.answer_timeout,
                profile=cfg.answer_profile,
                max_tokens=160,
            )
            if cfg.generate_answers
            else None
        )
        composer = AnswerComposer(answer_llm.complete if answer_llm else None)

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
                composer=composer,
            )
            for i in range(max(1, cfg.workers))
        ]
        application.state.workers = workers
        tasks = [
            asyncio.create_task(w.run(), name=f"assist-worker-{i}") for i, w in enumerate(workers)
        ]
        log.info(
            "Agent Assist 기동",
            extra={
                "kb_id": cfg.kb_id,
                "slm": cfg.use_slm,
                "answers": cfg.generate_answers,
                "workers": len(workers),
            },
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
            if answer_llm is not None:
                await answer_llm.aclose()
            await bus.close()

    return create_block_app(
        block_id=BLOCK_ID, title="VisionAI TA-ASSIST", settings=common, lifespan=lifespan
    )


app = create_app()


def main() -> None:
    serve(app, default_port=8088)


if __name__ == "__main__":
    main()
